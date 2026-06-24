Audit Report

## Title
Blocking RocksDB `shrink()` Under Tx-Pool Write Lock via Unauthenticated Rejected Transactions — (`tx-pool/src/component/recent_reject.rs`)

## Summary

An unprivileged attacker can flood the node with invalid transactions (e.g., referencing non-existent `OutPoint`s), each producing a `Reject::Resolve` that passes `should_recorded()` and triggers `RecentReject::put()`. Once `total_keys_num` exceeds `count_limit`, every subsequent `put()` calls `shrink()`, which executes synchronous RocksDB `drop_cf` + `create_cf_with_ttl` + five-shard key re-estimation while holding the tx-pool write lock. This blocks all concurrent honest transaction admission for the duration of each `shrink()` call.

## Finding Description

**`should_recorded()` admits all non-`Duplicated` rejections.** [1](#0-0) 
`Reject::Resolve` (produced by any transaction referencing a non-existent or dead cell) returns `true`, making it trivially exploitable with zero-fee, zero-PoW submissions.

**`put()` triggers `shrink()` on every insert once the limit is exceeded.** [2](#0-1) 
After `shrink()` runs, `self.total_keys_num` is updated to the post-shrink estimate. Because `shrink()` only clears one of `DEFAULT_SHARDS` (5) shards — removing ~20% of entries — if the attacker sustains insertions above that rate, `total_keys_num` remains above `count_limit` and every subsequent `put()` triggers another `shrink()`.

**`shrink()` performs synchronous, blocking RocksDB I/O.** [3](#0-2) 
`drop_cf`, `create_cf_with_ttl`, and `estimate_total_keys_num` (five shard property queries) are all synchronous RocksDB operations. They are not offloaded via `block_in_place` or any async mechanism.

**`put_recent_reject` acquires the tx-pool write lock independently.** [4](#0-3) 
`put_recent_reject` calls `self.tx_pool.write().await`, acquiring the same `tokio::sync::RwLock<TxPool>` used by all tx admission paths. While this lock is held, `shrink()` runs its blocking RocksDB operations, starving any concurrent `with_tx_pool_write_lock` call.

**`put_recent_reject` is called from `after_process` for every recordable rejection.** [5](#0-4) [6](#0-5) 
Both the remote and local rejection paths invoke `put_recent_reject` unconditionally when `reject.should_recorded()` is true, with no rate-limiting or cooldown.

**`TxPool` is held behind a `tokio::sync::RwLock`.** [7](#0-6) [8](#0-7) 
All honest tx admission paths (`submit_entry`, `after_process` conflict recording, etc.) contend on the same lock.

## Impact Explanation

While `shrink()` holds the write lock, no other transaction can be admitted to the pool. A sustained flood of invalid transactions keeps `total_keys_num` above `count_limit`, causing near-continuous write-lock occupation by blocking RocksDB I/O. This degrades or halts tx-pool admission for honest users across the network, matching the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

## Likelihood Explanation

The attack requires no transaction fees, no PoW, and no privileged access. Submitting transactions with random `OutPoint` inputs is trivially cheap and reliably produces `Reject::Resolve`. The attacker only needs to push `total_keys_num` past `count_limit` once; after that, the self-reinforcing loop (shrink removes 1/5 of entries, attacker keeps inserting) sustains the condition indefinitely. The attack is reachable via both P2P relay and the local RPC `send_transaction` endpoint.

## Recommendation

1. Move `recent_reject.put()` outside the tx-pool write lock — give `RecentReject` its own `Mutex` or `RwLock` so it does not contend with tx admission.
2. Wrap `shrink()` in `tokio::task::block_in_place` (already imported in `service.rs`) to avoid blocking the async runtime while holding any lock.
3. Add a cooldown/debounce on `shrink()` so it cannot be triggered more than once per configurable time window.
4. Rate-limit rejected transaction recording at the ingress point, before any lock is acquired.

## Proof of Concept

```
1. Attacker submits N transactions (N > count_limit) with random OutPoint inputs
   via P2P or RPC send_transaction.
2. Each tx → Reject::Resolve → should_recorded() = true → after_process calls
   put_recent_reject → acquires tx_pool.write() → recent_reject.put() called.
3. After count_limit insertions, every put() triggers shrink():
     drop_cf("random_shard")       // blocking RocksDB I/O, write lock held
     create_cf_with_ttl(...)       // blocking RocksDB I/O, write lock held
     estimate_total_keys_num()     // 5x RocksDB property queries, write lock held
4. Honest tx submissions block on tx_pool.write().await for the duration of
   each shrink().
5. Sustained attack → tx-pool write lock held near-continuously by shrink() →
   honest tx admission times out or is severely delayed.
```

### Citations

**File:** util/types/src/core/tx_pool.rs (L100-102)
```rust
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
```

**File:** tx-pool/src/component/recent_reject.rs (L62-69)
```rust
        if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
            if total_keys_num > self.count_limit {
                self.shrink()?;
            }
        } else {
            // overflow occurred, try shrink
            self.shrink()?;
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

**File:** tx-pool/src/process.rs (L258-267)
```rust
    pub(crate) async fn with_tx_pool_write_lock<U, F: FnMut(&mut TxPool, Arc<Snapshot>) -> U>(
        &self,
        mut f: F,
    ) -> (U, Arc<Snapshot>) {
        let mut tx_pool = self.tx_pool.write().await;
        let snapshot = tx_pool.cloned_snapshot();

        let ret = f(&mut tx_pool, Arc::clone(&snapshot));
        (ret, snapshot)
    }
```

**File:** tx-pool/src/process.rs (L428-438)
```rust
    pub(crate) async fn put_recent_reject(&self, tx_hash: &Byte32, reject: &Reject) {
        let mut tx_pool = self.tx_pool.write().await;
        if let Some(ref mut recent_reject) = tx_pool.recent_reject
            && let Err(e) = recent_reject.put(tx_hash, reject.clone())
        {
            error!(
                "Failed to record recent_reject {} {} {}",
                tx_hash, reject, e
            );
        }
    }
```

**File:** tx-pool/src/process.rs (L522-524)
```rust
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
```

**File:** tx-pool/src/process.rs (L548-550)
```rust
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
```

**File:** tx-pool/src/service.rs (L45-46)
```rust
use tokio::sync::{RwLock, mpsc};
use tokio::task::block_in_place;
```
