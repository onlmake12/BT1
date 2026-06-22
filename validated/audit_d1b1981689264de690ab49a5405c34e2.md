The code is right there. Let me trace the exact logic in `put()`: [1](#0-0) 

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    // ... db.put happens unconditionally ...
    if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
        if total_keys_num > self.count_limit {
            self.shrink()?;          // self.total_keys_num updated HERE (inside shrink)
        }
        // ← NO ELSE: total_keys_num is a LOCAL variable, never written back
    } else {
        self.shrink()?;
    }
    Ok(())
}
```

The bug is real and concrete:

- `self.total_keys_num.checked_add(1)` produces a **local** `total_keys_num`.
- When `total_keys_num <= self.count_limit`, the `if` body is skipped entirely — `self.total_keys_num` is **never updated**.
- `shrink()` is the only place that writes back to `self.total_keys_num` [2](#0-1) , but it is never reached.
- On every subsequent call, `self.total_keys_num.checked_add(1)` still returns the same stale value (the initial estimate from `build()`), so the condition `> count_limit` is never satisfied.

The DB write at line 60 happens unconditionally on every call [3](#0-2) , so the underlying `DBWithTTL` grows with every rejected tx, with no bound enforced.

---

### Title
`RecentReject::put` never increments `self.total_keys_num`, allowing unbounded disk growth via rejected transaction spam — (`tx-pool/src/component/recent_reject.rs`)

### Summary
`RecentReject::put` computes an incremented key count into a local variable but never writes it back to `self.total_keys_num`. As a result, `shrink()` is never triggered, and the underlying RocksDB (`DBWithTTL`) grows without bound as rejected transactions accumulate.

### Finding Description
In `put()` [4](#0-3) , the pattern is:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // self.total_keys_num is NOT updated here
}
```

`total_keys_num` is a local binding. The only write-back to `self.total_keys_num` occurs inside `shrink()` [2](#0-1) , which is never reached because the counter never advances. After `build()` initializes `total_keys_num` from a RocksDB estimate (typically 0 for a fresh node) [5](#0-4) , it stays at that value permanently.

### Impact Explanation
Every rejected transaction — regardless of reject reason — causes an unconditional `db.put()` [3](#0-2)  with no compensating `shrink()`. The `count_limit` invariant is completely broken. An attacker can exhaust disk space on a target node, causing a crash or preventing the node from processing further blocks/transactions (DoS).

### Likelihood Explanation
Any unprivileged peer can submit transactions that are rejected (e.g., `Reject::Full`, `Reject::Verification`, `Reject::Resolve`). No PoW, no keys, no special role required. The attacker only needs to generate distinct tx hashes at a rate faster than the TTL-based RocksDB compaction reclaims space.

### Recommendation
Write the incremented value back to `self.total_keys_num` in the non-shrink branch:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    } else {
        self.total_keys_num = total_keys_num;  // ← fix
    }
} else {
    self.shrink()?;
}
```

### Proof of Concept
```rust
// shard_num=2, count_limit=10, insert 10000 distinct rejected tx hashes
let mut rr = RecentReject::build(tmp_dir, 2, 10, 3600).unwrap();
let initial = rr.total_keys_num; // e.g. 0
for i in 0u64..10_000 {
    let hash = /* distinct Byte32 from i */;
    rr.put(&hash, Reject::Full).unwrap();
}
assert_eq!(rr.total_keys_num, initial); // still 0 — shrink never called
// DB on disk contains ~10000 entries, far beyond count_limit=10
```

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

**File:** tx-pool/src/component/recent_reject.rs (L110-111)
```rust
        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
```
