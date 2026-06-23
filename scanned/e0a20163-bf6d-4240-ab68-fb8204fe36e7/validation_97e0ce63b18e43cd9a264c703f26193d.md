The code is directly readable. Let me analyze the exact logic in `put()`:

```rust
// line 62-69
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // ← self.total_keys_num is NEVER written back here
} else {
    self.shrink()?;
}
```

The local binding `total_keys_num` is computed from `self.total_keys_num.checked_add(1)` but is **never assigned back to `self.total_keys_num`** in the non-shrink branch. `self.total_keys_num` is only updated inside `shrink()` at line 111 (`self.total_keys_num = total_keys_num`), which is never reached because the guard condition `total_keys_num > self.count_limit` always evaluates against the stale (initial) value.

---

### Title
`RecentReject::put` Never Persists Incremented Key Count — `count_limit` Eviction Permanently Disabled — (`tx-pool/src/component/recent_reject.rs`)

### Summary
`RecentReject::put` computes an incremented key count into a local variable but never writes it back to `self.total_keys_num`. Because the field stays at its initial estimate (typically 0 for a fresh DB), the `count_limit` guard is never satisfied, `shrink()` is never triggered, and the RocksDB backing store grows without bound for the lifetime of the node.

### Finding Description
In `put()`, the only place `self.total_keys_num` is ever updated is inside `shrink()`: [1](#0-0) 

But `shrink()` is only called when `total_keys_num > self.count_limit`: [2](#0-1) 

`total_keys_num` here is a **local `let` binding** — the result of `self.total_keys_num.checked_add(1)`. Because `self.total_keys_num` is never written back in the non-shrink branch, every subsequent call to `put()` re-evaluates the same stale value (e.g., `0 + 1 = 1`). With any `count_limit > 1`, the condition `1 > count_limit` is permanently false, so `shrink()` is never called and `self.total_keys_num` never advances past its initial estimate.

The initial estimate comes from `build()`, which calls `db.estimate_num_keys_cf` across all shards: [3](#0-2) 

For a fresh DB this is 0. For a restarted node it is an RocksDB estimate, but it is still never incremented after `build()` unless `shrink()` fires.

### Impact Explanation
1. **Disk exhaustion (DoS):** Every rejected transaction is written to the RocksDB store via `self.db.put(...)` at line 60, but the eviction path (`shrink()`) is never reached. The store grows without bound until TTL expiry. With a long TTL (the default is operator-configured and can be hours/days), an attacker can fill available disk space, crashing the node.
2. **`GetTotalRecentRejectNum` always returns the stale initial value:** `get_estimate_total_keys_num()` returns `self.total_keys_num` directly, which is frozen at the value set in `build()`. [4](#0-3) 

### Likelihood Explanation
The attacker path requires no privilege. Any peer or RPC caller can submit transactions that are rejected (wrong fee rate, invalid script, duplicate, etc.). Each rejection calls `put()`. The bug is triggered on the very first call and persists for the node's lifetime. No special timing, race condition, or configuration is required beyond `count_limit > 1` (the normal case).

### Recommendation
In the non-shrink branch of `put()`, write the incremented value back to `self.total_keys_num`:

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

### Proof of Concept
```rust
// Pseudocode unit test
let mut rr = RecentReject::build(tmp_dir, 5, /*count_limit=*/10, 3600).unwrap();
assert_eq!(rr.total_keys_num, 0);
for i in 0..100u32 {
    let hash = make_hash(i);
    rr.put(&hash, Reject::LowFeeRate(...)).unwrap();
}
// Bug: self.total_keys_num is still 0, shrink was never called
assert_eq!(rr.total_keys_num, 0);          // passes (demonstrates bug)
assert_eq!(rr.get_estimate_total_keys_num(), 0); // GetTotalRecentRejectNum returns 0
// DB actually contains 100 entries — count_limit=10 was never enforced
``` [5](#0-4)

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L39-52)
```rust
        let estimate_keys_num = cf_names
            .iter()
            .map(|cf| db.estimate_num_keys_cf(cf))
            .collect::<Result<Vec<_>, _>>()?;

        let total_keys_num = Self::checked_estimate_sum(&estimate_keys_num)?;

        Ok(RecentReject {
            shard_num,
            count_limit,
            ttl,
            db,
            total_keys_num,
        })
```

**File:** tx-pool/src/component/recent_reject.rs (L55-71)
```rust
    pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
        let hash_slice = hash.as_slice();
        let shard = self.get_shard(hash_slice).to_string();
        let reject: ckb_jsonrpc_types::PoolTransactionReject = reject.into();
        let json_string = serde_json::to_string(&reject)?;
        self.db.put(&shard, hash_slice, json_string)?;

        if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
            if total_keys_num > self.count_limit {
                self.shrink()?;
            }
        } else {
            // overflow occurred, try shrink
            self.shrink()?;
        }
        Ok(())
    }
```

**File:** tx-pool/src/component/recent_reject.rs (L80-82)
```rust
    pub fn get_estimate_total_keys_num(&self) -> u64 {
        self.total_keys_num
    }
```

**File:** tx-pool/src/component/recent_reject.rs (L110-111)
```rust
        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
```
