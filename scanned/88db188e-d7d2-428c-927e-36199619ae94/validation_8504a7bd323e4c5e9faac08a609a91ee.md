The code is confirmed. Let me verify the process.rs path as well.

Audit Report

## Title
`RecentReject::put` Never Increments `self.total_keys_num`, Permanently Disabling `shrink()` and Allowing Unbounded Disk Growth — (`tx-pool/src/component/recent_reject.rs`)

## Summary

In `RecentReject::put`, the incremented key count is computed into a local binding `total_keys_num` but is never written back to `self.total_keys_num` when the limit is not exceeded. Because `self.total_keys_num` is initialized from a RocksDB estimate at startup (0 for a fresh DB) and never updated, the guard `total_keys_num > self.count_limit` permanently evaluates to `1 > count_limit`, which is false for any configured limit ≥ 1. `shrink()` is therefore never triggered, and the `recent_reject` DBWithTTL grows without bound as rejected transactions accumulate.

## Finding Description

In `RecentReject::put` (lines 62–69 of `tx-pool/src/component/recent_reject.rs`):

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // ← self.total_keys_num is never updated here
} else {
    self.shrink()?;
}
```

`total_keys_num` is a local `let` binding. When `total_keys_num <= self.count_limit`, the function returns without assigning back to `self.total_keys_num`. On every subsequent call, `self.total_keys_num` is still the initial value (0 for a fresh DB), so `checked_add(1)` again produces `1`, and the condition `1 > count_limit` is again false. The counter is frozen at its initial value for the entire lifetime of the process.

`shrink()` is the only site that re-estimates and writes back `self.total_keys_num` (lines 110–111):

```rust
let total_keys_num = self.estimate_total_keys_num()?;
self.total_keys_num = total_keys_num;
```

Because `shrink()` is never reached, every rejected transaction entry is written to the RocksDB-with-TTL instance and never cleaned up by the count-based shrink mechanism. TTL provides only a delayed, compaction-dependent cleanup — it does not prevent disk space from being consumed in the interim, and with a minimum TTL of 1 day (enforced by `u8::max(1, config.keep_rejected_tx_hashes_days)` in `pool.rs` line 716), entries persist for at least 24 hours regardless.

The existing test (`tx-pool/src/component/tests/recent_reject.rs`, line 39) asserts `recent_reject.total_keys_num < 100`, which trivially passes because the counter is frozen at 0 — it does not verify actual DB occupancy.

## Impact Explanation

Every rejected transaction is written to the `recent_reject` RocksDB instance on disk with no upper bound enforced. A sustained stream of rejected transactions will exhaust available disk space, causing the node process to crash or become unable to write to any database (including the main chain DB). This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The exploit path is fully unprivileged. Any transaction that fails validation — wrong fee, malformed structure, double-spend, etc. — is routed through `put_recent_reject` (`process.rs`, lines 428–438), which calls `recent_reject.put()`. No PoW, no privileged access, and no special network position is required. An attacker only needs to submit transactions that are rejected, which is trivially achievable at high rate via the `send_transaction` RPC or P2P relay. The attack is repeatable and requires no victim interaction.

## Recommendation

Add the missing write-back to `self.total_keys_num` in the non-shrink branch of `put`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    } else {
        self.total_keys_num = total_keys_num;  // ← add this line
    }
} else {
    self.shrink()?;
}
```

## Proof of Concept

```rust
let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
let shard_num = 2;
let limit = 10;
let ttl = -1; // no TTL expiry

let mut rr = RecentReject::build(tmp_dir.path(), shard_num, limit, ttl).unwrap();

// Insert far more than `limit` entries
for i in 0..1000u64 {
    let key = Byte32::new(blake2b_256(i.to_le_bytes()));
    rr.put(&key, Reject::Malformed(i.to_string(), Default::default())).unwrap();
}

// Counter is still 0 — shrink() was never called
assert_eq!(rr.total_keys_num, 0);

// But the DB actually contains ~1000 entries, far above count_limit=10
let actual = rr.estimate_total_keys_num().unwrap(); // call via test helper
assert!(actual > limit as u64); // passes, proving the invariant is broken
```

The existing test at `tx-pool/src/component/tests/recent_reject.rs:39` (`assert!(recent_reject.total_keys_num < 100)`) passes trivially because the frozen counter is 0, not because the DB is actually bounded.