Audit Report

## Title
`RecentReject::put` Never Increments `self.total_keys_num`, Permanently Disabling `shrink()` and Allowing Unbounded Disk Growth — (File: tx-pool/src/component/recent_reject.rs)

## Summary

In `RecentReject::put`, the incremented key count is computed into a local variable `total_keys_num` but is never written back to `self.total_keys_num` when the limit is not exceeded. Because `self.total_keys_num` is never updated outside of `shrink()`, and `shrink()` is never triggered (since the guard condition never becomes true), the `recent_reject` RocksDB-with-TTL instance accumulates every rejected transaction entry indefinitely. This allows an unprivileged attacker to exhaust node disk space by submitting rejected transactions at high rate.

## Finding Description

In `put()` at lines 62–65 of `tx-pool/src/component/recent_reject.rs`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // self.total_keys_num is never updated here
}
```

`total_keys_num` is a local binding. When `total_keys_num <= self.count_limit`, the function returns without writing the incremented value back to `self.total_keys_num`. On every subsequent call, `self.total_keys_num` remains at its initial value (estimated from RocksDB at startup, typically 0 for a fresh DB), so `checked_add(1)` always produces the same local value (1), and the condition `1 > count_limit` is false for any configured limit ≥ 1.

The only site that updates `self.total_keys_num` is `shrink()` at lines 110–111:

```rust
let total_keys_num = self.estimate_total_keys_num()?;
self.total_keys_num = total_keys_num;
```

Since `shrink()` is never reached, `self.total_keys_num` is frozen at its initial value for the entire process lifetime, and the DB grows without any count-based bound.

The existing test at `tx-pool/src/component/tests/recent_reject.rs:39` asserts `recent_reject.total_keys_num < 100`, which trivially passes because the counter is frozen at 0 — it does not verify actual DB occupancy or that `shrink()` fired.

## Impact Explanation

Every rejected transaction is written to the `recent_reject` DBWithTTL on disk. With `shrink()` never firing, entries accumulate at the rate of rejections with no upper bound enforced by the count limit. RocksDB TTL provides only a delayed, compaction-dependent cleanup — it does not prevent disk space from being consumed in the interim. A sustained stream of rejected transactions (e.g., 1000/s at ~100 bytes each ≈ 8.6 GB/day) will exhaust available disk space, causing the node process to crash or become unable to write to any database. This matches the **High** impact: "Vulnerabilities which could easily crash a CKB node."

## Likelihood Explanation

The attacker path is fully unprivileged and reachable via standard transaction submission (RPC `send_transaction` or P2P relay). Any transaction that fails validation — wrong fee, malformed structure, double-spend, etc. — is routed through `put_recent_reject` in `tx-pool/src/process.rs` lines 428–438. No PoW, no privileged access, and no special network position is required. The attacker only needs to submit transactions that are rejected, which is trivially achievable at high rate with minimal cost (no valid fees need to be paid for rejected transactions).

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
let ttl = -1;

let mut rr = RecentReject::build(tmp_dir.path(), shard_num, limit, ttl).unwrap();

for i in 0..1000u64 {
    let key = Byte32::new(blake2b_256(i.to_le_bytes()));
    rr.put(&key, Reject::Malformed(i.to_string(), Default::default())).unwrap();
}

// Counter is still 0 (frozen), but DB contains 1000 entries
assert_eq!(rr.total_keys_num, 0);  // passes — counter never incremented
// Actual DB occupancy far exceeds count_limit=10, shrink() never fired
```

The existing test at `tx-pool/src/component/tests/recent_reject.rs:39` (`assert!(recent_reject.total_keys_num < 100)`) passes trivially because the counter is frozen at 0, not because the invariant is maintained.