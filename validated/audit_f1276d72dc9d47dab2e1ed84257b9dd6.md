The bug is real and concrete. Here is the analysis:

---

### Title
`RecentReject::put` Never Increments `self.total_keys_num` in the Non-Shrink Branch, Allowing Unbounded On-Disk Growth — (`tx-pool/src/component/recent_reject.rs`)

### Summary

`RecentReject::put` computes a local shadow variable `total_keys_num` via `checked_add(1)` but never writes it back to `self.total_keys_num` when the shrink threshold is not exceeded. As a result, `self.total_keys_num` is permanently stuck at its initialization value (typically `0` on a fresh or restarted node), the `count_limit` guard never fires, and the underlying `DBWithTTL` grows without bound for the lifetime of the process.

### Finding Description

In `put()`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;          // shrink() DOES write self.total_keys_num
    }
    // ← self.total_keys_num is NEVER assigned here
} else {
    self.shrink()?;
}
``` [1](#0-0) 

The local binding `total_keys_num` is computed but discarded. `self.total_keys_num` is only updated inside `shrink()`:

```rust
fn shrink(&mut self) -> Result<u64, AnyError> {
    ...
    self.total_keys_num = total_keys_num;   // only path that writes the field
    ...
}
``` [2](#0-1) 

Because `shrink()` is never reached (the guard `total_keys_num > self.count_limit` always evaluates `0 + 1 > count_limit`, which is false for any `count_limit >= 1`), `self.total_keys_num` stays at `0` forever on a fresh DB.

### Impact Explanation

Every rejected transaction (LowFeeRate, ExceededMaximumAncestorsCount, etc.) causes one `db.put()` call with no corresponding eviction. The `DBWithTTL` shard files grow without bound across the process lifetime. On a long-running mainnet node this translates to unbounded disk consumption and degraded I/O performance, matching the Medium scope.

### Likelihood Explanation

The attack path requires no privilege. Any peer or RPC caller can submit transactions that are rejected by the fee-rate check or other policy checks. The `recent_reject` store is written on every such rejection via `TxPool::put_recent_reject` → `RecentReject::put`. The bug is deterministic and reproducible on the first process restart with a fresh DB. [3](#0-2) 

### Recommendation

Add `self.total_keys_num = total_keys_num;` in the non-shrink branch:

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

1. Build `RecentReject` with `count_limit = 10` via `RecentReject::build(path, 5, 10, ttl)`.
2. Call `put()` 1000 times with distinct hashes and a `Reject::LowFeeRate` reason.
3. Assert `self.total_keys_num == 0` after every call (the field never advances).
4. Assert `estimate_total_keys_num()` returns ~1000 (the real DB count), far exceeding `count_limit`.
5. Observe that `shrink()` was never invoked. [4](#0-3)

### Citations

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
