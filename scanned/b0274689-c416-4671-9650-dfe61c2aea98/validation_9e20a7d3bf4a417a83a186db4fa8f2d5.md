### Title
Indexer `Pool.dead_cells` Not Updated on Block Rollback Causes Stale Live Cell Reporting — (`util/indexer-sync/src/pool.rs`)

### Summary

The `Pool` struct in `util/indexer-sync/src/pool.rs` maintains a `dead_cells: HashSet<OutPoint>` intended to track which cells are consumed by pending pool transactions, so the indexer can filter them from live-cell query results. The `PoolService::index_tx_pool` event loop only subscribes to `new_transaction` and `reject_transaction` notifications. The complementary cleanup path — `transactions_committed` — is called only from the indexer's `append()` method. Critically, the indexer's `rollback()` method (called during a chain reorg) never updates `dead_cells`, leaving the set in a stale state that does not reflect the actual pending-pool contents after the reorg.

### Finding Description

`Pool.dead_cells` has three update paths:

1. **Add** — `new_transaction()` inserts inputs of a newly pooled tx.
2. **Remove on reject** — `transaction_rejected()` removes inputs when a tx is rejected.
3. **Remove on commit** — `transactions_committed()` removes inputs when a block is appended by the indexer. [1](#0-0) 

The `PoolService::index_tx_pool` event loop subscribes only to `new_transaction` and `reject_transaction`: [2](#0-1) 

When a reorg occurs, the indexer calls `rollback()`. In both the classic indexer and the rich-indexer, `rollback()` rolls back the on-disk index state but performs **no pool update**: [3](#0-2) 

Compare with `append()`, which does call `transactions_committed`: [4](#0-3) 

After a rollback, transactions from the detached block are re-added to the tx-pool via `readd_detached_tx`. Whether those re-added transactions trigger a `notify_new_transaction` event (and thus re-populate `dead_cells`) is not guaranteed by the `Pool` struct's own invariants — the `Pool` has no rollback handler and no mechanism to re-synchronize with the pool after a reorg. The `dead_cells` set can therefore be missing entries for cells that are actually consumed by pending pool transactions.

The stale `dead_cells` set is then used directly in live-cell queries: [5](#0-4) [6](#0-5) 

### Impact Explanation

An RPC caller invoking `get_cells` or `get_cells_capacity` after a reorg may receive cells that are actually consumed by pending pool transactions, reported as live. This is the direct analog of the NFTPool `ownerToId` mapping becoming stale after a token transfer: a state-tracking data structure is not updated when the underlying state changes, causing incorrect ownership/liveness information to be returned to callers. Wallet software or dApps relying on the indexer for UTXO selection could attempt to spend already-consumed cells, resulting in rejected transactions or incorrect balance displays.

### Likelihood Explanation

Short reorgs (1–2 blocks) are a routine occurrence on any proof-of-work chain, including CKB mainnet. Any node running with `index_tx_pool = true` and serving indexer RPC is exposed. No special attacker capability is required; a normal network reorg is sufficient to trigger the stale state.

### Recommendation

Add a `rollback` or `clear` method to `Pool` and call it from both `Indexer::rollback()` and `AsyncRichIndexer::rollback()`. After clearing `dead_cells`, re-populate it from the current tx-pool snapshot, or subscribe to a post-reorg "pool re-populated" event. Alternatively, subscribe the `PoolService::index_tx_pool` loop to block-committed and reorg events so that `dead_cells` is always consistent with the actual pool state.

### Proof of Concept

1. Enable `index_tx_pool = true` in the node config.
2. Submit transaction Tx A consuming cell X. Confirm X appears in `dead_cells` and is excluded from `get_cells` results.
3. Mine a block that commits Tx A. The indexer's `append()` calls `transactions_committed`, removing X from `dead_cells`.
4. Trigger a reorg that detaches the block (e.g., via the `truncate` RPC or by connecting a longer competing chain). Tx A is re-added to the pool.
5. Query `get_cells` for the lock script owning X. Observe that X is now returned as a live cell even though it is consumed by the pending Tx A in the pool — the `dead_cells` set was not updated during `rollback()` and the re-added Tx A may not have re-triggered `notify_new_transaction`.

### Citations

**File:** util/indexer-sync/src/pool.rs (L20-62)
```rust
pub struct Pool {
    dead_cells: HashSet<OutPoint>,
}

impl Pool {
    /// the tx has been committed in a block, it should be removed from pending dead cells
    pub fn transaction_committed(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.remove(&input.previous_output());
        }
    }

    /// the tx has been rejected for some reason, it should be removed from pending dead cells
    pub fn transaction_rejected(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.remove(&input.previous_output());
        }
    }

    /// a new tx is submitted to the pool, mark its inputs as dead cells
    pub fn new_transaction(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.insert(input.previous_output());
        }
    }

    /// Return weather out_point referred cell consumed by pooled transaction
    pub fn is_consumed_by_pool_tx(&self, out_point: &OutPoint) -> bool {
        self.dead_cells.contains(out_point)
    }

    /// the txs has been committed in a block, it should be removed from pending dead cells
    pub fn transactions_committed(&mut self, txs: &[TransactionView]) {
        for tx in txs {
            self.transaction_committed(tx);
        }
    }

    /// return all dead cells
    pub fn dead_cells(&self) -> impl Iterator<Item = &OutPoint> {
        self.dead_cells.iter()
    }
}
```

**File:** util/indexer-sync/src/pool.rs (L116-136)
```rust
            let mut new_transaction_receiver = notify_controller
                .subscribe_new_transaction(SUBSCRIBER_NAME.to_string())
                .await;
            let mut reject_transaction_receiver = notify_controller
                .subscribe_reject_transaction(SUBSCRIBER_NAME.to_string())
                .await;

            loop {
                tokio::select! {
                    Some(tx_entry) = new_transaction_receiver.recv() => {
                        if let Some(pool) = service.pool.as_ref() {
                            pool.write().expect("acquire lock").new_transaction(&tx_entry.transaction);
                        }
                    }
                    Some((tx_entry, _reject)) = reject_transaction_receiver.recv() => {
                        if let Some(pool) = service.pool.as_ref() {
                            pool.write()
                            .expect("acquire lock")
                            .transaction_rejected(&tx_entry.transaction);
                        }
                    }
```

**File:** util/rich-indexer/src/indexer/mod.rs (L173-175)
```rust
        if let Some(mut pool) = self.pool.as_ref().map(|p| p.write().expect("acquire lock")) {
            pool.transactions_committed(&block.transactions());
        }
```

**File:** util/rich-indexer/src/indexer/mod.rs (L180-190)
```rust
    pub(crate) async fn rollback(&self) -> Result<(), Error> {
        let mut tx = self
            .store
            .transaction()
            .await
            .map_err(|err| Error::DB(err.to_string()))?;

        rollback_block(&mut tx).await?;

        tx.commit().await.map_err(|err| Error::DB(err.to_string()))
    }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L109-135)
```rust
        // filter cells in pool
        let mut dead_cells = Vec::new();
        if let Some(pool) = self
            .pool
            .as_ref()
            .map(|pool| pool.read().expect("acquire lock"))
        {
            dead_cells = pool
                .dead_cells()
                .map(|out_point| {
                    let tx_hash: H256 = out_point.tx_hash().into();
                    (tx_hash.as_bytes().to_vec(), out_point.index().into())
                })
                .collect::<Vec<(_, u32)>>()
        }
        if !dead_cells.is_empty() {
            let placeholders = dead_cells
                .iter()
                .map(|(_, output_index)| {
                    let placeholder = format!("(${}, {})", param_index, output_index);
                    param_index += 1;
                    placeholder
                })
                .collect::<Vec<_>>()
                .join(",");
            query_builder.and_where(format!("(tx_hash, output_index) NOT IN ({})", placeholders));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L73-102)
```rust
        // filter cells in pool
        let mut dead_cells = Vec::new();
        if let Some(pool) = self
            .pool
            .as_ref()
            .map(|pool| pool.read().expect("acquire lock"))
        {
            dead_cells = pool
                .dead_cells()
                .map(|out_point| {
                    let tx_hash: H256 = out_point.tx_hash().into();
                    (tx_hash.as_bytes().to_vec(), out_point.index().into())
                })
                .collect::<Vec<(_, u32)>>()
        }
        if !dead_cells.is_empty() {
            let placeholders = dead_cells
                .iter()
                .map(|(_, output_index)| {
                    let placeholder = format!("(${}, {})", param_index, output_index);
                    param_index += 1;
                    placeholder
                })
                .collect::<Vec<_>>()
                .join(",");
            query_builder.and_where(format!(
                "(ckb_transaction.tx_hash, output_index) NOT IN ({})",
                placeholders
            ));
        }
```
