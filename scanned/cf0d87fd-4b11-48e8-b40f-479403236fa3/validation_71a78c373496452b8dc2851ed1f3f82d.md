### Title
Tx-Pool Write Lock Held During Blocking RocksDB Column-Family Drop/Create in `RecentReject::shrink()` — (`tx-pool/src/component/recent_reject.rs`, `tx-pool/src/process.rs`)

### Summary

`put_recent_reject` acquires the tokio async `tx_pool` write lock and, while holding it, calls `RecentReject::put` which may invoke `shrink()`. `shrink()` executes synchronous, blocking RocksDB `drop_cf` + `create_cf_with_ttl` operations without `block_in_place` or `spawn_blocking`. An unprivileged remote peer can trigger this path by flooding cheaply-rejected transactions, causing the write lock to be held for the full duration of slow disk I/O and starving all concurrent tx admission, block assembly, and RBF checks.

---

### Finding Description

**Step 1 — `put_recent_reject` acquires the write lock and calls blocking code** [1](#0-0) 

`put_recent_reject` is an `async fn` that calls `self.tx_pool.write().await` and then, while holding the write guard, calls `recent_reject.put(tx_hash, reject.clone())`. There is no `block_in_place` or `spawn_blocking` wrapper anywhere in this path.

**Step 2 — `RecentReject::put` conditionally calls `shrink()`** [2](#0-1) 

After writing the entry to RocksDB, `put` checks whether `total_keys_num + 1 > count_limit`. If so, it calls `self.shrink()`. Crucially, `self.total_keys_num` is **not** incremented on each `put` call — it is only refreshed inside `shrink()` itself. This means once the cached count reaches `count_limit`, `shrink()` fires on every subsequent `put` call until the re-estimated count drops below the limit.

**Step 3 — `shrink()` performs blocking RocksDB column-family operations** [3](#0-2) 

`shrink()` calls `self.db.drop_cf(&shard)` and `self.db.create_cf_with_ttl(&shard, self.ttl)`. These are synchronous calls into RocksDB: [4](#0-3) 

Both `drop_cf` and `create_cf_with_ttl` are blocking disk I/O operations (RocksDB must flush, compact, and recreate the column family on-disk). They are called synchronously on the tokio executor thread, blocking it for the entire operation.

**Step 4 — The remote-peer call chain reaches `put_recent_reject`** [5](#0-4) 

`submit_remote_tx` enqueues the transaction into the verify queue. The verify manager (confirmed at `tx-pool/src/verify_mgr.rs`) dequeues and processes it, then calls `after_process`: [6](#0-5) 

When the rejection satisfies `reject.should_recorded()`, `put_recent_reject` is called, acquiring the write lock and potentially triggering `shrink()`.

**Step 5 — `with_tx_pool_write_lock` confirms the lock scope** [7](#0-6) 

The write lock is a tokio `RwLock`. Holding it across a blocking syscall blocks the tokio thread and prevents any other task from acquiring the lock for the full duration of the RocksDB operation.

---

### Impact Explanation

While the tx_pool write lock is held during `drop_cf`/`create_cf_with_ttl`:

- **Tx admission is blocked**: `submit_entry` calls `with_tx_pool_write_lock` and will stall waiting for the lock.
- **Block assembly is blocked**: `update_tx_pool_for_reorg` and `save_pool` both acquire the write lock.
- **RBF checks are blocked**: `check_rbf` runs inside `with_tx_pool_write_lock`.

RocksDB column-family drop/create can take tens to hundreds of milliseconds under load. With a sustained flood of cheaply-rejected transactions from a remote peer, `shrink()` fires repeatedly, causing near-continuous write-lock starvation. This degrades the node's ability to admit transactions and assemble blocks, constituting network-level congestion at low attacker cost (fee=0 transactions are trivially generated).

---

### Likelihood Explanation

- No privileged access required; any P2P peer can submit transactions.
- Transactions with fee=0 are trivially constructed and will be rejected cheaply (before script execution).
- The `count_limit` for `recent_reject` is a finite configured value; once reached, the attack sustains itself.
- The bug is structural: no existing guard prevents blocking I/O inside the write lock.

---

### Recommendation

1. **Move `put_recent_reject` outside the tx_pool write lock.** `RecentReject` is a separate RocksDB instance and does not need to be protected by the tx_pool lock. Give it its own `Mutex` or `RwLock`.
2. **Wrap `shrink()` in `tokio::task::block_in_place`** (or `spawn_blocking`) so the tokio executor thread is not blocked during RocksDB column-family operations.
3. **Update `total_keys_num` on every `put`** (not just after `shrink()`) to avoid repeated `shrink()` calls when the count is near the limit.

---

### Proof of Concept

```
1. Attacker connects as a P2P peer.
2. Attacker sends a stream of distinct transactions with fee=0 (LowFeeRate rejection).
3. Each rejected tx satisfying should_recorded() calls put_recent_reject.
4. Once total_keys_num >= count_limit, every put() call triggers shrink().
5. shrink() holds tx_pool.write() during drop_cf + create_cf_with_ttl.
6. Benchmark: measure tx_pool write lock hold time when shrink() fires vs. normal path.
   Expected: normal path ~microseconds; shrink() path ~10s–100s of milliseconds.
7. During shrink(), submit_entry / update_tx_pool_for_reorg / check_rbf all stall.
```

The call chain is fully traceable in production code with no test-only or privileged steps:

`submit_remote_tx` → `enqueue_verify_queue` → verify_mgr → `after_process` → `put_recent_reject` → `tx_pool.write()` → `recent_reject.put()` → `shrink()` → `drop_cf` / `create_cf_with_ttl` [3](#0-2) [1](#0-0)

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

**File:** tx-pool/src/process.rs (L519-524)
```rust
                                tx_hash: tx_hash.clone(),
                            });
                        }
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
