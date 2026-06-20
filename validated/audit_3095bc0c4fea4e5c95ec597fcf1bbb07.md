### Title
Missing `notify_new_transaction` Notification After Tx Status Demotion During Reorg — (`tx-pool/src/pool.rs`)

### Summary

During a chain reorganization, the `remove_by_detached_proposal` function in `tx-pool/src/pool.rs` silently moves transactions from `Gap`/`Proposed` status back to `Pending` without invoking the `callbacks.call_pending()` hook. This hook is the sole mechanism that triggers `notify_new_transaction` to RPC pub/sub subscribers. Off-chain clients (wallets, explorers, monitoring tools) subscribed to the `new_transaction` topic therefore receive no notification of this state transition and are left with a stale, incorrect view of transaction status.

### Finding Description

CKB's notification system (`ckb-notify`) exposes a pub/sub RPC interface. Subscribers to the `new_transaction` topic are notified via `callbacks.call_pending()`, which is wired to `notify_new_transaction` in `shared/src/shared_builder.rs`. [1](#0-0) 

The correct pattern for adding a transaction to the pending pool and notifying subscribers is `_submit_entry`: [2](#0-1) 

`_submit_entry` calls `callbacks.call_pending(&entry)` after a successful `add_pending`. This is used by `readd_detached_tx` (the path that re-adds detached block transactions after a reorg) and by normal transaction submission.

However, `remove_by_detached_proposal` — which handles the separate case of transactions whose *proposal* was detached by a reorg — calls `self.add_pending(entry)` directly, bypassing `_submit_entry` and never invoking `callbacks.call_pending()`: [3](#0-2) 

This function is called unconditionally inside `_update_tx_pool_for_reorg` on every reorg: [4](#0-3) 

The `Callbacks` struct has three hooks — `pending`, `proposed`, and `reject` — and all other state-change paths invoke the appropriate hook. Only `remove_by_detached_proposal` omits it: [5](#0-4) 

### Impact Explanation

Off-chain clients subscribed to `new_transaction` via the CKB RPC subscription system: [6](#0-5) 

will not receive a `new_transaction` push message when a reorg demotes a `Proposed` or `Gap` transaction back to `Pending`. The client's last known state for that transaction is `proposed_transaction` (or `new_transaction` from the original submission). After the reorg, the transaction is silently back in `Pending` with no notification. This causes:

- Wallets and explorers to display incorrect transaction status (showing "proposed" when the tx is actually pending again).
- Monitoring tools to miss the re-pending event entirely, potentially causing double-spend detection failures or incorrect fee-bump decisions.
- The `new_tip_block` notification fires correctly (the block is notified), but the corresponding tx-pool state change is invisible to subscribers.

### Likelihood Explanation

Chain reorgs are a routine occurrence on CKB mainnet, triggered by any block relayer submitting a valid competing chain. No privileged access, leaked keys, or majority hashpower is required. Any unprivileged peer that relays a valid competing block chain of greater total difficulty can trigger this code path. The impact scales with reorg depth: deeper reorgs demote more transactions without notification.

### Recommendation

In `remove_by_detached_proposal`, after a successful `add_pending`, invoke `callbacks.call_pending(&entry)` to match the behavior of `_submit_entry`. The fix should mirror the pattern already used in `_update_tx_pool_for_reorg` for the `proposed` and `gap` transitions:

```rust
// In remove_by_detached_proposal, after add_pending succeeds:
if let Ok((true, _evicts)) = self.add_pending(entry.clone()) {
    callbacks.call_pending(&entry);
}
```

The `callbacks` parameter must be threaded into `remove_by_detached_proposal` (currently it takes no `callbacks` argument), consistent with how `remove_committed_txs`, `remove_expired`, and `limit_size` all accept `callbacks`.

### Proof of Concept

1. Subscribe to `new_transaction` and `proposed_transaction` via the CKB RPC subscription endpoint.
2. Submit a transaction `T`. Observe `new_transaction` notification for `T`.
3. Mine a block that proposes `T`. Observe `proposed_transaction` notification for `T`.
4. Trigger a reorg that detaches the proposing block (e.g., submit a competing chain of greater total difficulty that does not include the proposal). `T`'s proposal is now in `detached_proposal_id`.
5. `_update_tx_pool_for_reorg` calls `tx_pool.remove_by_detached_proposal(detached_proposal_id.iter())`.
6. Inside `remove_by_detached_proposal`, `T` is found with `Status::Proposed`, removed, and re-added via `self.add_pending(entry)` — **no callback is fired**.
7. Observe: the subscriber receives **no** `new_transaction` notification for `T`, even though `T` is now back in the pending pool. The subscriber's last known state for `T` remains `proposed_transaction`, which is incorrect. [7](#0-6) [8](#0-7)

### Citations

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

**File:** tx-pool/src/callback.rs (L50-55)
```rust
    /// Call on after pending
    pub fn call_pending(&self, entry: &TxEntry) {
        if let Some(call) = &self.pending {
            call(entry)
        }
    }
```

**File:** rpc/src/module/subscription.rs (L214-239)
```rust
    fn subscribe(&self, topic: Topic) -> Result<Self::S> {
        let tx = match topic {
            Topic::NewTipHeader => self.new_tip_header_sender.clone(),
            Topic::NewTipBlock => self.new_tip_block_sender.clone(),
            Topic::NewTransaction => self.new_transaction_sender.clone(),
            Topic::ProposedTransaction => self.proposed_transaction_sender.clone(),
            Topic::RejectedTransaction => self.new_reject_transaction_sender.clone(),
            Topic::Log => self.log_sender.clone(),
        };
        let mut rx = tx.subscribe();
        Ok(Box::pin(async_stream::stream! {
                loop {
                    match rx.recv().await {
                        Ok(msg) => {
                            yield msg;
                        }
                        Err(RecvError::Lagged(cnt)) => {
                            error!("subscription lagged error: {:?}", cnt);
                        }
                        Err(RecvError::Closed) => {
                            break;
                        }
                    }
                }
        }))
    }
```
