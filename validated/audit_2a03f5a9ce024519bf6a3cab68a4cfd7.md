### Title
Silent Transaction State Transition from `Proposed`/`Gap` to `Pending` During Reorg Without Notification — (File: tx-pool/src/pool.rs)

### Summary
The `remove_by_detached_proposal` function in `tx-pool/src/pool.rs` silently transitions transactions from `Proposed` or `Gap` status back to `Pending` during a chain reorganization without invoking any callback or emitting any notification to off-chain subscribers. This is the direct CKB analog to the Solidity `_initialize` function setting `governor` without emitting `NewOwnership`.

### Finding Description

During a chain reorganization, `_update_tx_pool_for_reorg` (in `tx-pool/src/process.rs`) calls `tx_pool.remove_by_detached_proposal(detached_proposal_id.iter())`. [1](#0-0) 

The `remove_by_detached_proposal` function removes transactions from `Proposed` or `Gap` status and re-adds them to `Pending` via `self.add_pending(entry)`: [2](#0-1) 

Critically, unlike `_submit_entry`, which calls `callbacks.call_pending(&entry)` after a successful `add_pending`: [3](#0-2) 

`remove_by_detached_proposal` calls `add_pending` directly with no callback invocation. The registered `pending` callback is what triggers `notify_new_transaction` to all off-chain subscribers: [4](#0-3) 

The CKB subscription system exposes `proposed_transaction` and `new_transaction` topics to RPC subscribers: [5](#0-4) 

A subscriber receives a `proposed_transaction` event when a tx enters `Proposed` status. When a reorg reverts that tx to `Pending`, no corresponding `new_transaction` event is emitted. The subscriber's view of the transaction's state is now permanently stale until the tx is eventually rejected or re-proposed.

### Impact Explanation

Any RPC subscriber (wallet, indexer, monitoring tool) using the `proposed_transaction` subscription topic will receive a notification when a transaction is proposed. However, when a chain reorganization causes the detachment of the proposing block, the transaction silently reverts to `Pending` with no notification. The subscriber continues to believe the transaction is in `Proposed` status (imminent commitment), when in reality it has been de-proposed and must wait for re-proposal in a future block. This causes:

1. Wallets to not take corrective action (fee bumping, resubmission) because they believe the tx is still on track for commitment.
2. Indexers (e.g., `util/indexer-sync/src/pool.rs`) that track pool state via `subscribe_new_transaction` and `subscribe_reject_transaction` to hold a permanently incorrect view of the pool.
3. Monitoring tools to miss a critical state regression. [6](#0-5) 

### Likelihood Explanation

Chain reorganizations are a routine part of blockchain operation. Any block relayer or miner can submit a competing chain of equal or greater total difficulty, triggering `update_tx_pool_for_reorg` and the silent `remove_by_detached_proposal` path. No special privilege is required; this is a standard, externally reachable code path triggered by any peer submitting a valid competing block. [7](#0-6) 

### Recommendation

In `remove_by_detached_proposal`, after a successful `add_pending`, invoke the pending callback so that off-chain subscribers are notified of the state transition. The function signature should accept `callbacks: &Callbacks` (as other pool mutation functions do) and call `callbacks.call_pending(&entry)` when `add_pending` returns `Ok((true, _))`. [8](#0-7) 

### Proof of Concept

1. Connect to a CKB node via WebSocket and subscribe to both `proposed_transaction` and `new_transaction` topics.
2. Submit a transaction to the tx-pool; observe the `new_transaction` notification.
3. Mine a block that proposes the transaction; observe the `proposed_transaction` notification.
4. Submit a competing chain of greater length that does not include the proposing block, triggering a reorg.
5. Observe: no `new_transaction` notification is emitted for the transaction reverting to `Pending`. The subscriber's last known state for the transaction is `Proposed`, which is now incorrect.
6. The transaction will eventually expire or be evicted, at which point a `rejected_transaction` notification is emitted — but the intermediate `Pending` state was never communicated, leaving a gap in the subscriber's event log. [9](#0-8) [10](#0-9)

### Citations

**File:** tx-pool/src/process.rs (L1029-1035)
```rust
    if succ {
        match status {
            TxStatus::Fresh => callbacks.call_pending(&entry),
            TxStatus::Gap => callbacks.call_pending(&entry),
            TxStatus::Proposed => callbacks.call_proposed(&entry),
        }
    }
```

**File:** tx-pool/src/process.rs (L1039-1056)
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
```

**File:** tx-pool/src/pool.rs (L331-356)
```rust
    // remove transaction with detached proposal from gap and proposed
    // try re-put to pending
    pub(crate) fn remove_by_detached_proposal<'a>(
        &mut self,
        ids: impl Iterator<Item = &'a ProposalShortId>,
    ) {
        for id in ids {
            if let Some(e) = self.pool_map.get_by_id(id) {
                let status = e.status;
                if status == Status::Pending {
                    continue;
                }
                let mut entries = self.pool_map.remove_entry_and_descendants(id);
                entries.sort_unstable_by_key(|entry| entry.ancestors_count);
                for mut entry in entries {
                    let tx_hash = entry.transaction().hash();
                    entry.reset_statistic_state();
                    let ret = self.add_pending(entry);
                    debug!(
                        "remove_by_detached_proposal from {:?} {} add_pending {:?}",
                        status, tx_hash, ret
                    );
                }
            }
        }
    }
```

**File:** shared/src/shared_builder.rs (L559-566)
```rust
    tx_pool_builder.register_pending(Box::new(move |entry: &TxEntry| {
        // notify
        let notify_tx_entry = create_notify_entry(entry);
        notify_pending.notify_new_transaction(notify_tx_entry);
        let tx_hash = entry.transaction().hash();
        let entry_info = entry.to_info();
        fee_estimator_clone.accept_tx(tx_hash, entry_info);
    }));
```

**File:** rpc/src/module/subscription.rs (L100-110)
```rust
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

**File:** util/indexer-sync/src/pool.rs (L116-135)
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
```

**File:** chain/src/verify.rs (L386-398)
```rust
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
