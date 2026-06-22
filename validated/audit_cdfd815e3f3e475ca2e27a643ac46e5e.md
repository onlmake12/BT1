### Title
Notification Fired Before Tx-Pool State Is Fully Updated — Interaction-Before-Effect Ordering in Block Commit Path - (File: `chain/src/verify.rs`)

---

### Summary

In `chain/src/verify.rs`, after a new best block is committed to the database and the in-memory snapshot is updated, `notify_new_block` is dispatched to all external subscribers **before** the tx-pool reorg has been applied. The `update_tx_pool_for_reorg` call is a non-blocking `try_send` into an async channel; the actual pool mutation runs in a separate tokio task. Any RPC subscriber who receives the `new_tip_block` or `new_tip_header` event and immediately queries tx-pool state will observe a window of inconsistency where committed transactions still appear as pending, or detached transactions have not yet been re-added.

This is the direct CKB analog to the Solidity Check-Effects-Interactions violation described in the external report: an "interaction" (external notification) is emitted before all "effects" (tx-pool state update) are complete.

---

### Finding Description

In `ConsumeUnverifiedBlockProcessor` (`chain/src/verify.rs`), after a new best block is verified, the commit sequence is:

1. `db_txn.commit()?` — persists the block to RocksDB. [1](#0-0) 

2. `self.shared.store_snapshot(Arc::clone(&new_snapshot))` — atomically swaps the in-memory `Snapshot` via `ArcSwap`. [2](#0-1) 

3. `tx_pool_controller.update_tx_pool_for_reorg(...)` — this is implemented as a **non-blocking `try_send`** into the reorg channel. The actual pool mutation (removing committed txs, re-adding detached txs, updating proposals) runs asynchronously in a separate tokio task. [3](#0-2) 

4. `self.shared.notify_controller().notify_new_block(block.to_owned())` — fires the external `new_tip_block` / `new_tip_header` notification to all RPC subscribers **immediately**, without waiting for step 3 to complete. [4](#0-3) 

The `update_tx_pool_for_reorg` on the controller side uses `try_send` to a bounded channel, returning immediately: [5](#0-4) 

The actual pool mutation (`_update_tx_pool_for_reorg`) runs only when the reorg receiver task dequeues the message: [6](#0-5) 

This means there is a guaranteed window between the `notify_new_block` call and the completion of `_update_tx_pool_for_reorg`, during which the tx-pool state is inconsistent with the newly committed chain tip.

The CKB codebase itself acknowledges this race in test infrastructure: [7](#0-6) 

The `_submit_entry` function (for new transaction admission) correctly fires callbacks **after** the pool state is updated: [8](#0-7) 

But the block-commit path does not follow the same discipline.

The `notify_new_block` dispatches to all registered subscribers via spawned async tasks: [9](#0-8) 

Subscribers include WebSocket/TCP RPC clients subscribed to `new_tip_block`, `new_tip_header`, `new_transaction`, `proposed_transaction`, and `rejected_transaction` topics: [10](#0-9) 

---

### Impact Explanation

An unprivileged RPC subscriber who receives a `new_tip_block` event and immediately queries the node will observe a stale/inconsistent tx-pool state:

- **Committed transactions still appear as `pending`**: A tx included in the new block has not yet been removed from the pool by `remove_committed_txs`. [11](#0-10) 
- **Detached transactions not yet re-added**: During a reorg, txs from the detached fork have not yet been re-inserted into the pool by `readd_detached_tx`. [12](#0-11) 
- **Proposal state stale**: Transactions that should have transitioned from `pending` → `gap` → `proposed` have not yet done so. [13](#0-12) 

External clients (wallets, explorers, DApps, monitoring tools) that use the subscription-then-query pattern will receive incorrect tx status, incorrect pool contents, and incorrect fee estimates during this window. This matches the external report's stated impact: "confuse external clients about the state of the system."

---

### Likelihood Explanation

The subscription-then-query pattern is the standard usage of the CKB subscription API. Any wallet or monitoring tool that subscribes to `new_tip_block` and immediately calls `get_transaction` or `tx_pool_info` will reliably hit this window on every block. The window duration is bounded by the tokio task scheduler latency and the time to process `_update_tx_pool_for_reorg`, which grows with pool size and reorg depth. Under normal load this is milliseconds; under a large reorg it can be seconds.

---

### Recommendation

Move `notify_new_block` to fire **after** the tx-pool reorg is confirmed complete, rather than after only enqueuing the reorg message. One approach: make `update_tx_pool_for_reorg` a blocking call (awaited) before `notify_new_block` is dispatched, or introduce a completion signal that the notify path waits on. Alternatively, document clearly in the subscription API that `new_tip_block` events are delivered before the tx-pool is consistent with the new tip, and that callers must poll `tx_pool_info.tip_hash` to confirm consistency before relying on pool state — as the test infrastructure already does.

---

### Proof of Concept

1. Connect to a CKB node's WebSocket RPC endpoint.
2. Subscribe to `new_tip_block`:
   ```json
   {"id":1,"jsonrpc":"2.0","method":"subscribe","params":["new_tip_block"]}
   ```
3. Submit a transaction `T` to the pool; confirm it appears as `pending` via `get_transaction`.
4. Mine a block that commits `T`.
5. Immediately upon receiving the `new_tip_block` push notification, call:
   ```json
   {"id":2,"jsonrpc":"2.0","method":"get_transaction","params":["<T_hash>"]}
   ```
6. **Observed**: `T` is returned with status `pending` (still in pool), even though the notification announced the block that committed it.
7. **Expected**: `T` should be `committed` or absent from the pool.

The test helper `get_tip_tx_pool_info` in `test/src/node.rs` exists precisely to work around this race by polling until `tx_pool_info.tip_hash` matches the chain tip — confirming the inconsistency window is real and reproducible. [7](#0-6)

### Citations

**File:** chain/src/verify.rs (L359-359)
```rust
        db_txn.commit()?;
```

**File:** chain/src/verify.rs (L383-383)
```rust
            self.shared.store_snapshot(Arc::clone(&new_snapshot));
```

**File:** chain/src/verify.rs (L385-398)
```rust
            let tx_pool_controller = self.shared.tx_pool_controller();
            if tx_pool_controller.service_started() {
                if let Err(e) = tx_pool_controller.update_tx_pool_for_reorg(
                    fork.detached_blocks().clone(),
                    fork.attached_blocks().clone(),
                    fork.detached_proposal_id().clone(),
                    new_snapshot,
                ) {
                    error!("[verify block] notify update_tx_pool_for_reorg error {}", e);
                }
                if let Err(e) = tx_pool_controller.update_ibd_state(in_ibd) {
                    error!("Notify update_ibd_state error {}", e);
                }
            }
```

**File:** chain/src/verify.rs (L400-402)
```rust
            self.shared
                .notify_controller()
                .notify_new_block(block.to_owned());
```

**File:** tx-pool/src/service.rs (L241-258)
```rust
    pub fn update_tx_pool_for_reorg(
        &self,
        detached_blocks: VecDeque<BlockView>,
        attached_blocks: VecDeque<BlockView>,
        detached_proposal_id: HashSet<ProposalShortId>,
        snapshot: Arc<Snapshot>,
    ) -> Result<(), AnyError> {
        let notify = Notify::new((
            detached_blocks,
            attached_blocks,
            detached_proposal_id,
            snapshot,
        ));
        self.reorg_sender.try_send(notify).map_err(|e| {
            let (_m, e) = handle_try_send_error(e);
            e.into()
        })
    }
```

**File:** tx-pool/src/service.rs (L697-731)
```rust
        let signal_receiver = self.signal_receiver;
        self.handle.spawn(async move {
            loop {
                tokio::select! {
                    Some(message) = reorg_receiver.recv() => {
                        let Notify {
                            arguments: (detached_blocks, attached_blocks, detached_proposal_id, snapshot),
                        } = message;
                        let snapshot_clone = Arc::clone(&snapshot);
                        let detached_blocks_clone = detached_blocks.clone();
                        service.update_block_assembler_before_tx_pool_reorg(
                            detached_blocks_clone,
                            snapshot_clone
                        ).await;

                        let snapshot_clone = Arc::clone(&snapshot);
                        service
                        .update_tx_pool_for_reorg(
                            detached_blocks,
                            attached_blocks,
                            detached_proposal_id,
                            snapshot_clone,
                        )
                        .await;

                        service.update_block_assembler_after_tx_pool_reorg().await;
                    },
                    _ = signal_receiver.cancelled() => {
                        info!("TxPool reorg process service received exit signal, exit now");
                        break
                    },
                    else => break,
                }
            }
        });
```

**File:** test/src/node.rs (L509-527)
```rust
    /// The states of chain and txpool are updated asynchronously. Which means that the chain has
    /// updated to the newest tip but txpool not.
    /// get_tip_tx_pool_info wait to ensure the txpool update to the newest tip as well.
    pub fn get_tip_tx_pool_info(&self) -> TxPoolInfo {
        let tip_header = self.rpc_client().get_tip_header();
        let tip_hash = &tip_header.hash;
        let instant = Instant::now();
        let mut recent = TxPoolInfo::default();
        while instant.elapsed() < Duration::from_secs(10) {
            let tx_pool_info = self.rpc_client().tx_pool_info();
            if &tx_pool_info.tip_hash == tip_hash {
                return tx_pool_info;
            }
            recent = tx_pool_info;
        }
        panic!(
            "timeout to get_tip_tx_pool_info, tip_header={tip_header:?}, tx_pool_info: {recent:?}"
        );
    }
```

**File:** tx-pool/src/process.rs (L802-858)
```rust
    pub(crate) async fn update_tx_pool_for_reorg(
        &self,
        detached_blocks: VecDeque<BlockView>,
        attached_blocks: VecDeque<BlockView>,
        detached_proposal_id: HashSet<ProposalShortId>,
        snapshot: Arc<Snapshot>,
    ) {
        let mine_mode = self.block_assembler.is_some();
        let mut detached = LinkedHashSet::default();
        let mut attached = LinkedHashSet::default();

        let detached_headers: HashSet<Byte32> = detached_blocks
            .iter()
            .map(|blk| blk.header().hash())
            .collect();

        for blk in detached_blocks {
            detached.extend(blk.transactions().into_iter().skip(1))
        }

        for blk in attached_blocks {
            self.fee_estimator.commit_block(&blk);
            attached.extend(blk.transactions().into_iter().skip(1));
        }
        let retain: Vec<TransactionView> = detached.difference(&attached).cloned().collect();

        let fetched_cache = self.fetch_txs_verify_cache(retain.iter()).await;

        // If there are any transactions requires re-process, return them.
        //
        // At present, there is only one situation:
        // - If the hardfork was happened, then re-process all transactions.
        {
            // This closure is used to limit the lifetime of mutable tx_pool.
            let mut tx_pool = self.tx_pool.write().await;

            _update_tx_pool_for_reorg(
                &mut tx_pool,
                &attached,
                &detached_headers,
                detached_proposal_id,
                snapshot,
                &self.callbacks,
                mine_mode,
            );

            // notice: readd_detached_tx don't update cache
            self.readd_detached_tx(&mut tx_pool, retain, fetched_cache)
                .await;
        }

        self.remove_orphan_txs_by_attach(&attached).await;
        {
            let mut queue = self.verify_queue.write().await;
            queue.remove_txs(attached.iter().map(|tx| tx.proposal_short_id()));
        }
    }
```

**File:** tx-pool/src/process.rs (L1016-1037)
```rust
fn _submit_entry(
    tx_pool: &mut TxPool,
    status: TxStatus,
    entry: TxEntry,
    callbacks: &Callbacks,
) -> Result<HashSet<TxEntry>, Reject> {
    let tx_hash = entry.transaction().hash();
    debug!("submit_entry {:?} {}", status, tx_hash);
    let (succ, evicts) = match status {
        TxStatus::Fresh => tx_pool.add_pending(entry.clone())?,
        TxStatus::Gap => tx_pool.add_gap(entry.clone())?,
        TxStatus::Proposed => tx_pool.add_proposed(entry.clone())?,
    };
    if succ {
        match status {
            TxStatus::Fresh => callbacks.call_pending(&entry),
            TxStatus::Gap => callbacks.call_pending(&entry),
            TxStatus::Proposed => callbacks.call_proposed(&entry),
        }
    }
    Ok(evicts)
}
```

**File:** tx-pool/src/process.rs (L1039-1113)
```rust
fn _update_tx_pool_for_reorg(
    tx_pool: &mut TxPool,
    attached: &LinkedHashSet<TransactionView>,
    detached_headers: &HashSet<Byte32>,
    detached_proposal_id: HashSet<ProposalShortId>,
    snapshot: Arc<Snapshot>,
    callbacks: &Callbacks,
    mine_mode: bool,
) {
    tx_pool.snapshot = Arc::clone(&snapshot);

    // NOTE: `remove_by_detached_proposal` will try to re-put the given expired/detached proposals into
    // pending-pool if they can be found within txpool. As for a transaction
    // which is both expired and committed at the one time(commit at its end of commit-window),
    // we should treat it as a committed and not re-put into pending-pool. So we should ensure
    // that involves `remove_committed_txs` before `remove_expired`.
    tx_pool.remove_committed_txs(attached.iter(), callbacks, detached_headers);
    tx_pool.remove_by_detached_proposal(detached_proposal_id.iter());

    // mine mode:
    // pending ---> gap ----> proposed
    // try move gap to proposed
    if mine_mode {
        let mut proposals = Vec::new();
        let mut gaps = Vec::new();

        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) {
            let short_id = entry.inner.proposal_short_id();
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push((short_id, entry.inner.clone()));
            }
        }

        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Pending) {
            let short_id = entry.inner.proposal_short_id();
            let elem = (short_id.clone(), entry.inner.clone());
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push(elem);
            } else if snapshot.proposals().contains_gap(&short_id) {
                gaps.push(elem);
            }
        }

        for (id, entry) in proposals {
            debug!("begin to proposed: {:x}", id);
            if let Err(e) = tx_pool.proposed_rtx(&id) {
                debug!(
                    "Failed to add proposed tx {}, reason: {}",
                    entry.transaction().hash(),
                    e
                );
                callbacks.call_reject(tx_pool, &entry, e);
            } else {
                callbacks.call_proposed(&entry)
            }
        }

        for (id, entry) in gaps {
            debug!("begin to gap: {:x}", id);
            if let Err(e) = tx_pool.gap_rtx(&id) {
                debug!(
                    "Failed to add tx to gap {}, reason: {}",
                    entry.transaction().hash(),
                    e
                );
                callbacks.call_reject(tx_pool, &entry, e.clone());
            }
        }
    }

    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
```

**File:** notify/src/lib.rs (L261-299)
```rust
    fn handle_notify_new_block(&self, block: BlockView) {
        trace!("New block event {:?}", block);
        let block_hash = block.hash();
        // notify all subscribers
        for subscriber in self.new_block_subscribers.values() {
            let block = block.clone();
            let subscriber = subscriber.clone();
            self.handle.spawn(async move {
                if let Err(e) = subscriber.send(block).await {
                    error!("Failed to notify new block, error: {}", e);
                }
            });
        }

        // notify all watchers
        for watcher in self.new_block_watchers.values() {
            if let Err(e) = watcher.send(block_hash.clone()) {
                error!("Failed to notify new block watcher, error: {}", e);
            }
        }

        // notify script
        if let Some(script) = self.config.new_block_notify_script.clone() {
            let script_timeout = self.timeout.script;
            self.handle.spawn(async move {
                let args = [format!("{block_hash:#x}")];
                match timeout(script_timeout, Command::new(&script).args(&args).status()).await {
                    Ok(ret) => match ret {
                        Ok(status) => debug!("The new_block_notify script exited with: {status}"),
                        Err(e) => error!(
                            "Failed to run new_block_notify_script: {} {:?}, error: {}",
                            script, args[0], e
                        ),
                    },
                    Err(_) => ckb_logger::warn!("new_block_notify_script {script} timed out"),
                }
            });
        }
    }
```

**File:** rpc/src/module/subscription.rs (L84-110)
```rust
    /// ##### Topics
    ///
    /// ###### `new_tip_header`
    ///
    /// Whenever there's a block that is appended to the canonical chain, the CKB node will publish the
    /// block header to subscribers.
    ///
    /// The type of the `params.result` in the push message is [`HeaderView`](../../ckb_jsonrpc_types/struct.HeaderView.html).
    ///
    /// ###### `new_tip_block`
    ///
    /// Whenever there's a block that is appended to the canonical chain, the CKB node will publish the
    /// whole block to subscribers.
    ///
    /// The type of the `params.result` in the push message is [`BlockView`](../../ckb_jsonrpc_types/struct.BlockView.html).
    ///
    /// ###### `new_transaction`
    ///
    /// Subscribers will get notified when a new transaction is submitted to the pool.
    ///
    /// The type of the `params.result` in the push message is [`PoolTransactionEntry`](../../ckb_jsonrpc_types/struct.PoolTransactionEntry.html).
    ///
    /// ###### `proposed_transaction`
    ///
    /// Subscribers will get notified when an in-pool transaction is proposed by chain.
    ///
    /// The type of the `params.result` in the push message is [`PoolTransactionEntry`](../../ckb_jsonrpc_types/struct.PoolTransactionEntry.html).
```

**File:** tx-pool/src/pool.rs (L223-241)
```rust
    pub(crate) fn remove_committed_txs<'a>(
        &mut self,
        txs: impl Iterator<Item = &'a TransactionView>,
        callbacks: &Callbacks,
        detached_headers: &HashSet<Byte32>,
    ) {
        for tx in txs {
            let tx_hash = tx.hash();
            debug!("try remove_committed_tx {}", tx_hash);
            self.remove_committed_tx(tx, callbacks);

            self.committed_txs_hash_cache
                .put(tx.proposal_short_id(), tx_hash);
        }

        if !detached_headers.is_empty() {
            self.resolve_conflict_header_dep(detached_headers, callbacks)
        }
    }
```
