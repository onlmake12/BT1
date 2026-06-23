Based on my analysis of the actual code, here is my assessment:

---

### Title
Repeated `shrink()` Under Tx-Pool Write Lock via Unauthenticated Rejected Transactions — (`tx-pool/src/component/recent_reject.rs`)

### Summary

An unprivileged attacker can submit a high volume of transactions with invalid inputs (e.g., referencing non-existent cells), causing repeated `Reject::Resolve` rejections. Each rejection passes `should_recorded()` and triggers `RecentReject::put()`. Once `total_keys_num > count_limit`, every subsequent `put()` calls `shrink()`, which executes `drop_cf` + `create_cf_with_ttl` + key re-estimation against RocksDB — all while holding the tx-pool write lock. This blocks honest transaction admission for the duration of each `shrink()` call.

### Finding Description

**`should_recorded()` admits all non-Duplicated rejections:** [1](#0-0) 

`Reject::Resolve`, `Reject::Expiry`, `Reject::Full`, and `Reject::RBFRejected` all return `true`. Submitting transactions with random/invalid input `OutPoint`s costs nothing and reliably produces `Reject::Resolve`.

**`put()` calls `shrink()` on every insert once the limit is exceeded:** [2](#0-1) 

Once `total_keys_num > count_limit`, the condition is re-evaluated on every `put()`. Because `shrink()` only clears one of `DEFAULT_SHARDS` (5) shards — removing ~20% of entries — if the attacker has pushed the count to ≥ 1.25× the limit, every single subsequent `put()` triggers another `shrink()`.

**`shrink()` performs blocking RocksDB operations:** [3](#0-2) 

`drop_cf` removes all files in a column family; `create_cf_with_ttl` opens a new one; `estimate_total_keys_num` queries all 5 shards. These are synchronous RocksDB I/O operations.

**`recent_reject` is a field of `TxPool`, which is held behind a `tokio::sync::RwLock`:** [4](#0-3) [5](#0-4) 

All `recent_reject.put()` calls in `process.rs` occur while the caller holds the pool write lock. Every `shrink()` invocation therefore holds the write lock for the full duration of the RocksDB CF drop/create cycle.

### Impact Explanation

While the write lock is held in `shrink()`, no other transaction can be admitted to the pool. If the attacker sustains a submission rate that keeps `total_keys_num` above `count_limit`, every honest transaction submission blocks behind a `shrink()` call. This degrades or halts tx-pool admission, preventing honest users from getting transactions into the pool and disrupting CKB economic activity.

### Likelihood Explanation

The attack requires no fees, no PoW, and no privileged access. Submitting transactions with random `OutPoint` inputs is trivially cheap. The attacker only needs to push `total_keys_num` past `count_limit` once; after that, the self-reinforcing loop (shrink removes 1/5 of entries, but attacker keeps inserting) sustains the condition indefinitely.

### Recommendation

1. Move `recent_reject` operations **outside** the tx-pool write lock — e.g., use a separate `Mutex<RecentReject>` or perform the write asynchronously after releasing the pool lock.
2. Add a cooldown/debounce on `shrink()` so it cannot be triggered more than once per time window.
3. Rate-limit rejected transaction recording at the ingress point (before acquiring the write lock).
4. Consider making `shrink()` asynchronous or offloading it to a background task.

### Proof of Concept

```
1. Attacker submits N transactions (N > count_limit) with random OutPoint inputs via P2P or RPC.
2. Each tx is rejected with Reject::Resolve → should_recorded() = true → recent_reject.put() called.
3. After count_limit insertions, every put() triggers shrink():
     drop_cf("random_shard")       // RocksDB I/O, write lock held
     create_cf_with_ttl(...)       // RocksDB I/O, write lock held
     estimate_total_keys_num()     // 5x RocksDB property queries, write lock held
4. Honest tx submissions block on pool.write().await for the duration of each shrink().
5. Sustained attack → tx-pool write lock held near-continuously → honest tx admission times out.
``` [6](#0-5) [3](#0-2)

### Citations

**File:** util/types/src/core/tx_pool.rs (L100-102)
```rust
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
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

**File:** tx-pool/src/pool.rs (L44-44)
```rust
    pub recent_reject: Option<RecentReject>,
```

**File:** tx-pool/src/service.rs (L45-45)
```rust
use tokio::sync::{RwLock, mpsc};
```
