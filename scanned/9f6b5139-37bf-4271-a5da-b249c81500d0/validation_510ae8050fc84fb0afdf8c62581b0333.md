The code in `recent_reject.rs` confirms the claim exactly. Lines 62–65 show:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // self.total_keys_num is never written back
}
```

`self.total_keys_num` is initialized from `estimate_num_keys_cf` (0 for a fresh DB) and only written inside `shrink()` at line 111. Since `shrink()` is never triggered (counter stays at 0, `0+1=1` never exceeds `count_limit=10_000_000`), the DB grows without bound. The exploit path via `put_recent_reject` → `RecentReject::put()` is reachable by any unprivileged user submitting rejected transactions. The finding is valid.

---

Audit Report

## Title
`RecentReject::put()` Never Increments `total_keys_num`, Disabling `shrink()` and Allowing Unbounded DB Growth — (`tx-pool/src/component/recent_reject.rs`)

## Summary
In `RecentReject::put()`, the result of `self.total_keys_num.checked_add(1)` is bound to a local variable but never assigned back to `self.total_keys_num`. Because the counter permanently stays at its initialization value (0 for a fresh DB), the condition `total_keys_num > self.count_limit` is never satisfied, `shrink()` is never called, and the reject DB (`DBWithTTL`) grows without bound. An unprivileged remote attacker can exploit this by continuously submitting rejected transactions to exhaust disk space and crash the node.

## Finding Description
**Root cause:** In `tx-pool/src/component/recent_reject.rs`, `put()` (lines 62–69):

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // ← self.total_keys_num is NEVER updated here
} else {
    self.shrink()?;
}
```

`self.total_keys_num` has exactly two write sites:
1. `build()` (lines 44–51): initialized from `estimate_num_keys_cf`, which returns 0 for a fresh DB.
2. `shrink()` (lines 110–111): reset to the post-drop estimate — but `shrink()` is never reached.

Because `self.total_keys_num` stays at 0, every call evaluates `0 + 1 = 1`. With the default `count_limit` of 10,000,000 (`util/app-config/src/legacy/tx_pool.rs`, line 58), the guard `1 > 10_000_000` is always false. `shrink()` is never invoked, and every rejected transaction appends a JSON-serialized `PoolTransactionReject` record to the DB permanently (until RocksDB TTL compaction, which is not guaranteed to run promptly).

**Exploit flow:**
1. Attacker submits transactions that fail validation (double-spend, malformed, insufficient fee — any `Reject` variant where `should_recorded()` returns true).
2. `after_process()` in `tx-pool/src/process.rs` (lines 522–524 / 548–550) calls `put_recent_reject()`.
3. `put_recent_reject()` (lines 428–438) calls `recent_reject.put()`.
4. Each call writes to the DB and increments nothing; the DB grows indefinitely.
5. TTL (`keep_rejected_tx_hashes_days`, default 7 days) provides no real-time protection — RocksDB only reclaims TTL-expired entries during compaction, which the attacker can outpace.
6. Disk exhaustion causes the node to crash or become unable to write to any DB.

**Existing guards reviewed and found insufficient:**
- The `count_limit` guard is structurally correct but rendered inert by the missing assignment.
- TTL-based expiry is deferred to compaction and does not bound write rate.
- No rate limiting on rejected transactions is applied at the `put_recent_reject` call sites.

## Impact Explanation
Disk exhaustion halts all DB writes on the node, stopping block processing and chain sync — a full node crash/DoS. This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
The attacker requires no privileges, no PoW, no keys, and no special role. Any transaction that fails validation (e.g., a double-spend referencing a known spent output) triggers the vulnerable path. The attack is fully deterministic, reproducible on any fresh node, and can be executed via the public `send_transaction` RPC or P2P relay. The bug is present on every node running this code.

## Recommendation
Add the missing assignment in the non-shrink branch of `put()` in `tx-pool/src/component/recent_reject.rs`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;  // ← add this line
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

No change is needed in the `shrink()` branch, as `self.total_keys_num` is already reset there via `estimate_total_keys_num()`.

## Proof of Concept
```rust
let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
let count_limit = 10u64;
let mut rr = RecentReject::build(tmp_dir.path(), 2, count_limit, -1).unwrap();

// Insert far beyond count_limit
for i in 0..100u64 {
    let key = Byte32::new(blake2b_256(i.to_le_bytes()));
    rr.put(&key, Reject::Malformed(i.to_string(), Default::default())).unwrap();
}

// Counter never moved; shrink() was never called
assert_eq!(rr.total_keys_num, 0);
// DB holds ~100 entries despite count_limit=10
```

The counter stays at 0 throughout all 100 insertions. `shrink()` is never invoked. The DB holds all entries despite `count_limit = 10`, confirming unbounded growth.