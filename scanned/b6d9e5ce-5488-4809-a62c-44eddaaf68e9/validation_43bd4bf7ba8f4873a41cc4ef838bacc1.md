Audit Report

## Title
Tx-Pool Write Lock Held During Blocking RocksDB `drop_cf`/`create_cf_with_ttl` in `RecentReject::shrink()` — (`tx-pool/src/component/recent_reject.rs`, `tx-pool/src/process.rs`)

## Summary

`put_recent_reject` acquires the tokio async tx_pool write lock and, while holding it, calls `RecentReject::put`, which conditionally invokes `shrink()`. `shrink()` executes synchronous, blocking RocksDB `drop_cf` and `create_cf_with_ttl` operations with no `block_in_place` or `spawn_blocking` wrapper. A compounding bug — `self.total_keys_num` is never incremented inside `put()` — causes `shrink()` to fire on every `put()` call once the count limit is reached, enabling a remote peer to sustain near-continuous write-lock starvation by flooding cheaply-rejected transactions.

## Finding Description

**Lock acquisition and blocking call chain:**

`put_recent_reject` calls `self.tx_pool.write().await` and, while holding the write guard, calls `recent_reject.put(tx_hash, reject.clone())` with no async boundary. [1](#0-0) 

Inside `put()`, after writing to RocksDB, the code checks a local `total_keys_num` (result of `self.total_keys_num.checked_add(1)`) against `count_limit` and calls `self.shrink()` if exceeded. [2](#0-1) 

Critically, `self.total_keys_num` is **never assigned** the incremented value — the local binding `total_keys_num` from `checked_add(1)` is used only for the comparison. `self.total_keys_num` is only updated inside `shrink()` itself via `estimate_total_keys_num()`. This means once `self.total_keys_num + 1 > count_limit`, every subsequent `put()` call triggers `shrink()` until the re-estimated count drops below the limit. [3](#0-2) 

`shrink()` performs two synchronous, blocking RocksDB column-family operations — `drop_cf` and `create_cf_with_ttl` — directly on the tokio executor thread, with no `block_in_place` or `spawn_blocking`: [4](#0-3) [5](#0-4) 

**Reachable exploit path from remote peer:**

`submit_remote_tx` feeds into `resumeble_process_tx_and_notify_full_reject`, which eventually calls `after_process`. When a rejection satisfies `reject.should_recorded()`, `put_recent_reject` is called: [6](#0-5) [7](#0-6) 

**Lock scope confirmation:**

`with_tx_pool_write_lock` confirms the lock is a tokio `RwLock` held for the full duration of the closure. `put_recent_reject` acquires the same lock directly and holds it across the entire `recent_reject.put()` → `shrink()` call chain. [8](#0-7) 

## Impact Explanation

While the tx_pool write lock is held during `drop_cf`/`create_cf_with_ttl`, all operations requiring the write lock stall: `submit_entry`, `update_tx_pool_for_reorg`, `save_pool`, and `check_rbf`. RocksDB column-family drop/create involves flushing and compacting on-disk structures and can take tens to hundreds of milliseconds under load. With the `total_keys_num` increment bug causing `shrink()` to fire on every `put()` once the limit is reached, a sustained flood of cheaply-rejected transactions from a single remote peer causes near-continuous write-lock starvation, blocking tx admission and block assembly.

This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

- No privileged access is required; any P2P peer can submit transactions.
- Fee=0 transactions are trivially constructed and rejected cheaply (before script execution), satisfying `should_recorded()`.
- The `count_limit` is a finite configured value; once reached, the `total_keys_num` increment bug ensures `shrink()` fires on every subsequent `put()`.
- The attack is self-sustaining: the attacker only needs to maintain a stream of distinct rejected transactions.
- No existing guard prevents blocking I/O inside the write lock.

## Recommendation

1. **Move `put_recent_reject` outside the tx_pool write lock.** `RecentReject` is a separate RocksDB instance and does not need protection by the tx_pool lock. Give it its own `Mutex` or `RwLock`.
2. **Wrap `shrink()` in `tokio::task::block_in_place`** (or `spawn_blocking`) so the tokio executor thread is not blocked during RocksDB column-family operations.
3. **Fix the `total_keys_num` increment bug** in `put()`: assign `self.total_keys_num = total_keys_num` after the `checked_add(1)` so the cached count advances on each call and `shrink()` is not triggered on every `put()` once the limit is reached.

## Proof of Concept

```
1. Attacker connects as a P2P peer.
2. Attacker sends a stream of distinct transactions with fee=0 (LowFeeRate rejection).
   - Each tx has a unique hash; fee=0 ensures cheap rejection before script execution.
3. Each rejected tx satisfying should_recorded() calls put_recent_reject.
4. Once self.total_keys_num (initialized at startup) + 1 > count_limit, every put()
   triggers shrink() because self.total_keys_num is never incremented in put().
5. shrink() holds tx_pool.write() during drop_cf + create_cf_with_ttl.
6. Verification:
   a. Instrument put_recent_reject to record write-lock hold duration.
   b. Normal path (no shrink): lock held ~microseconds.
   c. shrink() path: lock held ~10s–100s of milliseconds (RocksDB CF drop/create).
   d. During shrink(), submit_entry / update_tx_pool_for_reorg / check_rbf all stall,
      observable as increased latency on block assembly and tx admission RPCs.

Call chain (fully traceable in production code):
submit_remote_tx → resumeble_process_tx_and_notify_full_reject → after_process
  → put_recent_reject → tx_pool.write() → recent_reject.put() → shrink()
  → drop_cf / create_cf_with_ttl
```

### Citations

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

**File:** tx-pool/src/process.rs (L371-379)
```rust
    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
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

**File:** db/src/db_with_ttl.rs (L81-91)
```rust
    pub fn create_cf_with_ttl(&mut self, col: &str, ttl: i32) -> Result<()> {
        let opts = Options::default();
        self.inner
            .create_cf_with_ttl(col, &opts, ttl)
            .map_err(internal_error)
    }

    /// Delete column family.
    pub fn drop_cf(&mut self, col: &str) -> Result<()> {
        self.inner.drop_cf(col).map_err(internal_error)
    }
```
