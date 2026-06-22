### Title
Infinite Busy-Loop DoS in `try_loop_sync` When `indexer.append()` Fails Persistently — (File: `util/indexer-sync/src/lib.rs`)

---

### Summary

`IndexerSyncService::try_loop_sync` contains a `loop` that retries `indexer.append(&block)` indefinitely with no sleep, no backoff, and no break when the call fails. Because a failed `append` does not advance the indexer tip, every subsequent iteration fetches and retries the exact same block, creating a tight CPU-spinning infinite loop. This permanently stalls the indexer sync service and the async task awaiting it.

---

### Finding Description

In `util/indexer-sync/src/lib.rs`, `try_loop_sync` (lines 136–200) drives the indexer forward one block at a time:

```
loop {
    match indexer.tip() {
        Ok(Some((tip_number, tip_hash))) => {
            match self.get_block_by_number(tip_number + 1) {
                Some(block) => {
                    if block.parent_hash() == tip_hash {
                        if let Err(e) = indexer.append(&block) {
                            error!("Failed to append block: {}. Will attempt to retry.", e);
                            // ← NO sleep, NO break, NO backoff
                        }
                    } else { /* rollback */ }
                }
                None => { break; }
            }
        }
        Ok(None) => {
            if let Err(e) = indexer.append(&block) {   // genesis block
                error!("Failed to append block: {}. Will attempt to retry.", e);
                // ← same problem
            }
        }
        Err(e) => {
            error!("Failed to get tip: {}", e);
            // ← loop continues immediately
        }
    }
}
```

When `indexer.append(&block)` returns `Err`, the indexer tip is **not updated** (the write was not committed). The loop immediately re-enters, calls `indexer.tip()` again, receives the same `tip_number`, fetches the same block, and calls `append` again — forever. The comment "Will attempt to retry" confirms this is intentional retry logic, but there is no rate-limiting mechanism of any kind.

The identical defect exists in all three error arms:
- `append` failure on a normal block (line 160–162) [1](#0-0) 
- `append` failure on the genesis block (line 186–188) [2](#0-1) 
- `tip()` failure (line 195–197) [3](#0-2) 

`spawn_poll` calls `try_loop_sync` inside `spawn_blocking` and **awaits** the result before processing the next new-block notification: [4](#0-3) 

If `try_loop_sync` never returns, the awaiting async task is permanently blocked, and no further block-notification events are ever consumed.

---

### Impact Explanation

A persistent `append` failure causes:

1. **Tight CPU spin** — the blocking OS thread allocated by `spawn_blocking` runs at 100 % on one core with no yield point.
2. **Indexer sync permanently halted** — the async task awaiting the `spawn_blocking` future never resumes; no subsequent blocks are ever indexed.
3. **RPC query staleness** — all `get_cells`, `get_transactions`, and related indexer RPC methods return stale data indefinitely, silently misleading downstream users and applications.

The core consensus node is unaffected, but the indexer service — which is the primary interface for wallets, dApps, and explorers — is completely DoS'd.

---

### Likelihood Explanation

The trigger is any condition that makes `indexer.append()` return `Err` for a specific block height and then continue to do so on every retry. Realistic causes include:

- **Rich indexer (SQL path):** A SQL constraint violation, a deadlock, or a connection-pool exhaustion triggered by specific block content (e.g., an unusually large number of outputs, a script args field that exceeds a column width, or a duplicate-key scenario in `append_block`). An unprivileged block relayer that broadcasts a consensus-valid block with such content to the node causes the indexer to fail on that block permanently. [5](#0-4) 
- **Basic indexer (RocksDB path):** A RocksDB write error (e.g., disk-full, column-family corruption) on the indexer's secondary DB path. [6](#0-5) 
- **Transient-turned-permanent errors:** Any transient DB error (network partition to a remote SQL backend, temporary lock contention) that persists across retries will trigger the spin because there is no backoff to allow the condition to clear.

The entry path for an unprivileged attacker is: relay a consensus-valid block → node stores it → indexer attempts to index it → `append` fails → loop spins forever.

---

### Recommendation

Add a sleep/backoff in every error arm of `try_loop_sync`, or `break` out of the loop and rely on the outer polling interval to retry:

```rust
if let Err(e) = indexer.append(&block) {
    error!("Failed to append block: {}", e);
    // Option A: break and let the poll interval retry
    break;
    // Option B: sleep before retrying
    // std::thread::sleep(Duration::from_secs(1));
}
```

The same fix must be applied to the genesis-block arm and the `tip()` error arm. Breaking out of the loop is preferable because `spawn_poll` already has a polling interval (`poll_interval`) and a new-block watcher that will re-invoke `try_loop_sync` after a delay, providing natural backoff without busy-waiting. [7](#0-6) 

---

### Proof of Concept

1. Enable the CKB rich-indexer (SQL backend).
2. Relay or mine a block whose content causes `AsyncRichIndexer::append` to return `Err` (e.g., trigger a SQL unique-constraint violation by inserting a duplicate block hash, or exhaust the SQL connection pool).
3. Observe that the thread running `try_loop_sync` immediately re-enters the loop, calls `indexer.tip()`, receives the same tip height, fetches the same block, and calls `append` again — spinning at 100 % CPU with no pause.
4. Observe that the async task in `spawn_poll` that `await`s the `spawn_blocking` future never resumes.
5. Issue any indexer RPC call (`get_cells`, `get_transactions`): responses are permanently stale at the block height just before the failing block.

### Citations

**File:** util/indexer-sync/src/lib.rs (L160-162)
```rust
                                if let Err(e) = indexer.append(&block) {
                                    error!("Failed to append block: {}. Will attempt to retry.", e);
                                }
```

**File:** util/indexer-sync/src/lib.rs (L184-188)
```rust
                Ok(None) => match self.get_block_by_number(0) {
                    Some(block) => {
                        if let Err(e) = indexer.append(&block) {
                            error!("Failed to append block: {}. Will attempt to retry.", e);
                        }
```

**File:** util/indexer-sync/src/lib.rs (L195-197)
```rust
                Err(e) => {
                    error!("Failed to get tip: {}", e);
                }
```

**File:** util/indexer-sync/src/lib.rs (L232-254)
```rust
            let mut new_block_watcher = notify_controller.watch_new_block(subscriber_name).await;
            let mut interval = time::interval(poll_service.poll_interval);
            interval.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
            loop {
                let indexer = indexer_service.clone();
                tokio::select! {
                    Ok(_) = new_block_watcher.changed() => {
                        let service = poll_service.clone();
                        if let Err(e) = async_handle.spawn_blocking(move || {
                            service.try_loop_sync(indexer)
                        }).await {
                            error!("{} syncing join error {:?}", indexer_service.get_identity(), e);
                        }
                        new_block_watcher.borrow_and_update();
                    },
                    _ = interval.tick() => {
                        let service = poll_service.clone();
                        if let Err(e) = async_handle.spawn_blocking(move || {
                            service.try_loop_sync(indexer)
                        }).await {
                            error!("{} syncing join error {:?}", indexer_service.get_identity(), e);
                        }
                    }
```

**File:** util/rich-indexer/src/indexer/mod.rs (L156-178)
```rust
    pub(crate) async fn append(&self, block: &BlockView) -> Result<(), Error> {
        let mut tx = self
            .store
            .transaction()
            .await
            .map_err(|err| Error::DB(err.to_string()))?;
        if self.custom_filters.is_block_filter_match(block) {
            let block_id = append_block(block, &mut tx).await?;
            self.insert_transactions(block_id, block, &mut tx).await?;
        } else {
            let block_headers = vec![(block.hash().raw_data().to_vec(), block.number() as i64)];
            bulk_insert_blocks_simple(block_headers, &mut tx).await?;
        }
        tx.commit()
            .await
            .map_err(|err| Error::DB(err.to_string()))?;

        if let Some(mut pool) = self.pool.as_ref().map(|p| p.write().expect("acquire lock")) {
            pool.transactions_committed(&block.transactions());
        }

        Ok(())
    }
```

**File:** util/indexer/src/indexer.rs (L317-330)
```rust
    fn append(&self, block: &BlockView) -> Result<(), Error> {
        let mut batch = self.store.batch()?;
        let transactions = block.transactions();
        let pool = self.pool.as_ref().map(|p| p.write().expect("acquire lock"));
        if !self.custom_filters.is_block_filter_match(block) {
            batch.put_kv(Key::Header(block.number(), &block.hash(), true), vec![])?;
            batch.commit()?;

            if let Some(mut pool) = pool {
                pool.transactions_committed(&transactions);
            }

            return Ok(());
        }
```
