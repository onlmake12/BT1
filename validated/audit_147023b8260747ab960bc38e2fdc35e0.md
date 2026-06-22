### Title
`RecentReject.total_keys_num` Never Updated After Each `put()` — `count_limit` Protection Permanently Bypassed - (File: `tx-pool/src/component/recent_reject.rs`)

---

### Summary

In `tx-pool/src/component/recent_reject.rs`, the `put()` function computes a new `total_keys_num` value but never writes it back to `self.total_keys_num`. As a result, the in-memory counter stays frozen at its initialization value for the entire lifetime of the node. The `count_limit` guard that is supposed to trigger `shrink()` and bound the size of the rejected-transaction database never fires, allowing any unprivileged transaction submitter to grow the on-disk `recent_reject` RocksDB store without bound.

---

### Finding Description

`RecentReject` is the tx-pool subsystem that records recently-rejected transactions so that peers cannot re-relay them. It uses a sharded `DBWithTTL` (RocksDB with per-key TTL) and maintains an in-memory counter `total_keys_num` to decide when to call `shrink()` (which drops a random shard and re-creates it, bounding disk usage).

The counter is initialized correctly in `build()`:

```rust
// tx-pool/src/component/recent_reject.rs:44
let total_keys_num = Self::checked_estimate_sum(&estimate_keys_num)?;
``` [1](#0-0) 

But in `put()`, the updated count is computed into a **local variable** and the result is never assigned back to `self.total_keys_num`:

```rust
// tx-pool/src/component/recent_reject.rs:62-69
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;          // only path that updates self.total_keys_num
    }
    // ← self.total_keys_num is NEVER updated here
} else {
    self.shrink()?;
}
``` [2](#0-1) 

`self.total_keys_num` is only mutated inside `shrink()`:

```rust
// tx-pool/src/component/recent_reject.rs:110-111
let total_keys_num = self.estimate_total_keys_num()?;
self.total_keys_num = total_keys_num;
``` [3](#0-2) 

Because `self.total_keys_num` never grows, the condition `total_keys_num > self.count_limit` evaluates to `(initial_estimate + 1) > count_limit`. For a fresh node where `initial_estimate = 0`, this is `1 > count_limit`, which is false for any sane limit. `shrink()` is therefore never called, and the DB grows without bound.

The stale counter is also the value returned to the RPC layer:

```rust
// tx-pool/src/service.rs:1100-1106
async fn get_total_recent_reject_num(&self) -> Option<u64> {
    let tx_pool = self.tx_pool.read().await;
    tx_pool.recent_reject.as_ref()
        .map(|r| r.get_estimate_total_keys_num())
}
``` [4](#0-3) 

```rust
// tx-pool/src/component/recent_reject.rs:80-82
pub fn get_estimate_total_keys_num(&self) -> u64 {
    self.total_keys_num   // always returns the stale initial value
}
``` [5](#0-4) 

This is the exact same class of bug as the reported issue: a cached accounting value is updated in one direction (incremented in the original; here, never incremented at all) but the write-back is missing, so the guard that depends on it never fires.

---

### Impact Explanation

The `count_limit` is the only in-process mechanism that bounds the size of the `recent_reject` store. With it permanently disabled:

1. **Disk exhaustion DoS**: Every rejected transaction is written to the store and never evicted by the `shrink()` path. The store grows at the rate of rejections × TTL. An attacker who can generate cheap-to-reject transactions (fee-too-low, duplicate, malformed) can fill the node's disk.
2. **Stale RPC metric**: `total_recent_reject_num` reported by the `get_overview` terminal RPC always returns the frozen initial value, misleading operators. [6](#0-5) 

---

### Likelihood Explanation

Any unprivileged peer or RPC caller can submit transactions. Transactions rejected for cheap reasons (fee below minimum, already-known hash, capacity underflow, etc.) are recorded in `recent_reject` without requiring script execution. A single attacker with a standard CKB wallet can generate thousands of such submissions per second, each consuming disk space with no bound enforced by the broken counter. No special privilege, key, or majority hash-power is required. [2](#0-1) 

---

### Recommendation

In `put()`, write the incremented value back to `self.total_keys_num` before the limit check:

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    let hash_slice = hash.as_slice();
    let shard = self.get_shard(hash_slice).to_string();
    let reject: ckb_jsonrpc_types::PoolTransactionReject = reject.into();
    let json_string = serde_json::to_string(&reject)?;
    self.db.put(&shard, hash_slice, json_string)?;

    if let Some(new_total) = self.total_keys_num.checked_add(1) {
        self.total_keys_num = new_total;          // ← missing assignment
        if self.total_keys_num > self.count_limit {
            self.shrink()?;
        }
    } else {
        self.shrink()?;
    }
    Ok(())
}
``` [2](#0-1) 

---

### Proof of Concept

1. Start a CKB node with a non-zero `recent_reject` count limit (the default).
2. Repeatedly submit transactions whose fee rate is below `min_fee_rate` via the `send_transaction` RPC. Each is rejected and written to the `recent_reject` DB.
3. Observe that `get_overview` always reports `total_recent_reject_num` equal to the value at node startup (frozen), and that the on-disk `recent_reject` directory grows monotonically without any shard being dropped, until disk space is exhausted. [2](#0-1) [4](#0-3)

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

**File:** tx-pool/src/component/recent_reject.rs (L104-113)
```rust
    fn shrink(&mut self) -> Result<u64, AnyError> {
        let mut rng = thread_rng();
        let shard = rng.sample(Uniform::new(0, self.shard_num)).to_string();
        self.db.drop_cf(&shard)?;
        self.db.create_cf_with_ttl(&shard, self.ttl)?;

        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
        Ok(total_keys_num)
    }
```

**File:** tx-pool/src/service.rs (L1100-1106)
```rust
    async fn get_total_recent_reject_num(&self) -> Option<u64> {
        let tx_pool = self.tx_pool.read().await;
        tx_pool
            .recent_reject
            .as_ref()
            .map(|r| r.get_estimate_total_keys_num())
    }
```
