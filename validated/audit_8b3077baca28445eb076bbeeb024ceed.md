The code confirms the claim. In `put()` at lines 62–65:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // self.total_keys_num is never written back here
} else {
    self.shrink()?;
}
```

`total_keys_num` is a local binding. `self.total_keys_num` is only updated inside `shrink()` at lines 110–111, which is never reached because the guard condition never becomes true. The test at line 39 (`assert!(recent_reject.total_keys_num < 100)`) passes trivially since the counter is frozen at its initial estimated value (0 for a fresh DB), not because `shrink()` fired.

---

Audit Report

## Title
`RecentReject::put` Never Writes Back Incremented `total_keys_num`, Permanently Disabling `shrink()` and Allowing Unbounded Disk Growth — (File: tx-pool/src/component/recent_reject.rs)

## Summary
In `RecentReject::put`, the incremented key count is computed into a local variable `total_keys_num` but is never written back to `self.total_keys_num` when the count limit is not exceeded. Because `self.total_keys_num` is never updated outside of `shrink()`, and `shrink()` is never triggered (the guard condition always evaluates against the frozen initial value), the `recent_reject` RocksDB-with-TTL instance accumulates every rejected transaction entry indefinitely. A sustained stream of rejected transactions can exhaust node disk space and crash the node.

## Finding Description
In `tx-pool/src/component/recent_reject.rs` lines 62–69:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // self.total_keys_num is never updated here
} else {
    self.shrink()?;
}
```

`total_keys_num` is a local binding produced by `checked_add(1)`. When `total_keys_num <= self.count_limit`, the function returns without writing the incremented value back to `self.total_keys_num`. On every subsequent call, `self.total_keys_num` remains at its initial value (estimated from RocksDB at startup via `build()` lines 39–52, typically 0 for a fresh DB). Thus `checked_add(1)` always produces the same local value (1), and `1 > count_limit` is false for any configured limit ≥ 1.

The only site that updates `self.total_keys_num` is `shrink()` at lines 110–111:
```rust
let total_keys_num = self.estimate_total_keys_num()?;
self.total_keys_num = total_keys_num;
```
Since `shrink()` is never reached, `self.total_keys_num` is frozen at its initial value for the entire process lifetime, and the DB grows without any count-based bound. RocksDB TTL provides only compaction-dependent, delayed cleanup — it does not prevent disk space from being consumed in the interim.

The existing test at `tx-pool/src/component/tests/recent_reject.rs:39` asserts `recent_reject.total_keys_num < 100`, which trivially passes because the counter is frozen at 0 — it does not verify actual DB occupancy or that `shrink()` fired.

## Impact Explanation
Every rejected transaction is written to the `recent_reject` DBWithTTL on disk. With `shrink()` never firing, entries accumulate at the rate of rejections with no upper bound enforced by the count limit. A sustained stream of rejected transactions will exhaust available disk space, causing the node process to crash or become unable to write to any database. This matches **High** impact: "Vulnerabilities which could easily crash a CKB node."

## Likelihood Explanation
The attacker path is fully unprivileged and reachable via standard transaction submission (RPC `send_transaction` or P2P relay). Any transaction that fails validation is routed through `put_recent_reject`. No PoW, no privileged access, and no special network position is required. Rejected transactions require no valid fees, making high-rate submission essentially free for the attacker.

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
let mut rr = RecentReject::build(tmp_dir.path(), 2, 10, -1).unwrap();

for i in 0..1000u64 {
    let key = Byte32::new(blake2b_256(i.to_le_bytes()));
    rr.put(&key, Reject::Malformed(i.to_string(), Default::default())).unwrap();
}

// Counter is still 0 (frozen), but DB contains 1000 entries
assert_eq!(rr.total_keys_num, 0);  // passes — counter never incremented
// Actual DB occupancy far exceeds count_limit=10; shrink() never fired
```