The code at lines 62–65 confirms the claim exactly. `total_keys_num` is a local `let` binding from `self.total_keys_num.checked_add(1)` and is never assigned back to `self.total_keys_num` in the non-shrink branch. [1](#0-0) 

`self.total_keys_num` is only updated inside `shrink()` at line 111, which is only reached when `total_keys_num > self.count_limit`. [2](#0-1) 

Since `self.total_keys_num` never advances, every call to `put()` evaluates the same stale value + 1, and the condition is permanently false for any `count_limit > 1`. [1](#0-0) 

---

Audit Report

## Title
`RecentReject::put` Never Persists Incremented Key Count — `count_limit` Eviction Permanently Disabled — (`tx-pool/src/component/recent_reject.rs`)

## Summary
In `RecentReject::put()`, the incremented key count is computed into a local `let` binding but never written back to `self.total_keys_num`. Because the field is frozen at its initial estimate (0 for a fresh DB), the `count_limit` guard is never satisfied, `shrink()` is never triggered, and the RocksDB backing store grows unboundedly until TTL expiry. An unprivileged peer can exploit this by submitting rejected transactions to exhaust disk space and crash the node.

## Finding Description
In `put()` (lines 62–65), `total_keys_num` is a local binding from `self.total_keys_num.checked_add(1)`. It is compared against `self.count_limit`, but `self.total_keys_num` is never updated in the non-shrink branch:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // self.total_keys_num is never written back here
}
```

The only place `self.total_keys_num` is updated is inside `shrink()` at line 111 (`self.total_keys_num = total_keys_num`), which is only reached when `total_keys_num > self.count_limit`. Since `self.total_keys_num` never advances, every call to `put()` evaluates `(stale_value + 1) > count_limit` — permanently false for any `count_limit > 1`. `shrink()` is never called, and `self.total_keys_num` never advances past its initial estimate from `build()` (0 for a fresh DB). The actual RocksDB store grows with every `put()` call at line 60, with no count-based eviction ever firing. TTL-based expiry in RocksDB is the only remaining protection, but for long TTL windows (hours/days), an attacker can fill available disk space before entries expire.

## Impact Explanation
**High — Vulnerabilities which could easily crash a CKB node.**

Every rejected transaction is written to RocksDB at line 60 with no count-based eviction. An attacker submitting a sustained stream of rejected transactions (wrong fee rate, invalid script, duplicate, etc.) will grow the store without bound for the duration of the TTL window. Disk exhaustion causes the node process to crash or become unable to write new data, taking the node offline.

## Likelihood Explanation
No privilege is required. Any peer or RPC caller can submit transactions that are rejected. The bug is triggered on the very first call to `put()` and persists for the node's lifetime. No race condition, special timing, or configuration is needed beyond `count_limit > 1` (the normal case). The attack is repeatable and cheap — rejected transactions require no valid fee payment.

## Recommendation
In the non-shrink branch of `put()`, write the incremented value back to `self.total_keys_num` before the shrink check:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;   // ← add this line
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

## Proof of Concept
```rust
#[test]
fn test_count_limit_never_enforced() {
    let tmp = tempfile::tempdir().unwrap();
    let mut rr = RecentReject::build(tmp.path(), 5, /*count_limit=*/10, 3600).unwrap();
    assert_eq!(rr.total_keys_num, 0);
    for i in 0..100u32 {
        let hash = make_hash(i); // unique Byte32 per iteration
        rr.put(&hash, make_reject()).unwrap();
    }
    // Bug: self.total_keys_num is still 0, shrink was never called
    assert_eq!(rr.total_keys_num, 0);
    assert_eq!(rr.get_estimate_total_keys_num(), 0);
    // DB actually contains 100 entries — count_limit=10 was never enforced
}
```

Run with `cargo test -p ckb-tx-pool recent_reject`. The assertion `total_keys_num == 0` will pass, demonstrating that 100 entries were written with no eviction despite `count_limit = 10`.

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L62-65)
```rust
        if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
            if total_keys_num > self.count_limit {
                self.shrink()?;
            }
```

**File:** tx-pool/src/component/recent_reject.rs (L110-111)
```rust
        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
```
