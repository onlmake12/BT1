Audit Report

## Title
`RecentReject::put` Never Increments `self.total_keys_num`, Disabling the `count_limit` Guard — (`tx-pool/src/component/recent_reject.rs`)

## Summary
In `RecentReject::put`, the result of `self.total_keys_num.checked_add(1)` is bound to a local variable `total_keys_num` that is never written back to `self.total_keys_num`. For a node starting with an empty DB (initial estimate = 0), every `put()` call evaluates `0 + 1 > count_limit`, which is always false, so `shrink()` is never triggered and the on-disk `DBWithTTL` grows without bound within the TTL window. An unprivileged attacker who continuously submits recordable-rejected transactions can exhaust node disk, causing a crash.

## Finding Description
In `tx-pool/src/component/recent_reject.rs` lines 62–69:

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

`total_keys_num` is a local binding. `self.total_keys_num` is only mutated inside `shrink()` (lines 110–111), which is never reached because the guard is always false. At startup, `self.total_keys_num` is initialized from RocksDB's `estimate_num_keys_cf` (lines 39–51); for a fresh/empty DB this is 0. Every subsequent `put()` call computes `0 + 1 = 1`, checks `1 > count_limit` (default 10,000,000 — always false), and returns without shrinking or updating the counter. The existing unit test at line 39 (`assert!(recent_reject.total_keys_num < 100)`) passes vacuously — it asserts the stale counter (0) is less than the limit (100) after inserting 160 entries, and does not detect the unbounded DB growth.

## Impact Explanation
The `recent_reject` RocksDB (`DBWithTTL`) grows without bound across the node's lifetime, bounded only by TTL expiry (default 7 days). The intended invariant — that `keep_rejected_tx_hashes_count` caps disk usage — is completely bypassed. Sustained submission of rejected transactions exhausts node disk, causing a crash or service disruption. This matches the allowed CKB bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation
The attacker entry point is any unprivileged peer or RPC caller. `should_recorded()` returns `true` for every `Reject` variant except `Duplicated` (line 100–102 of `util/types/src/core/tx_pool.rs`). The easiest vector is submitting transactions with random non-existent outpoints, rejected as `Reject::Resolve(OutPointError::Unknown(...))` — no UTXOs, no fees, just valid CKB transaction serialization. These flow through `after_process` → `put_recent_reject` → `recent_reject.put()` (lines 522–524 of `tx-pool/src/process.rs`). The default `count_limit` is 10,000,000 entries, trivially exceeded within the 7-day TTL window at any sustained submission rate.

## Recommendation
Add `self.total_keys_num = total_keys_num;` in the non-shrink branch of `put()`:

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

Also update the unit test to assert that the actual RocksDB key count (not `total_keys_num`) stays within `count_limit` after many puts.

## Proof of Concept
The existing test in `tx-pool/src/component/tests/recent_reject.rs` already demonstrates the bug: it inserts 160 entries with `limit = 100`, then asserts `recent_reject.total_keys_num < 100` at line 39 — this passes only because `total_keys_num` is never incremented (stays at 0), not because shrinking worked. A confirming test:

```rust
#[test]
fn test_count_limit_enforced() {
    let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
    let mut rr = RecentReject::build(tmp_dir.path(), 2, 10u64, -1).unwrap();
    for i in 0..100u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        rr.put(&key, Reject::Malformed(i.to_string(), Default::default())).unwrap();
    }
    // BUG: total_keys_num is still 0 — never incremented
    assert_eq!(rr.total_keys_num, 0);
    // BUG: actual DB key count far exceeds limit of 10
    let actual = rr.estimate_total_keys_num().unwrap();
    assert!(actual > 10, "DB has {} keys, limit is 10", actual);
}
```

Running this against the current code confirms `total_keys_num == 0` throughout and the DB holding ~100 entries despite `limit = 10`.