### Title
Missing `rejected_transaction` Notification After `remove_transaction` RPC Removes Transactions from the Pool - (File: `tx-pool/src/pool.rs`)

### Summary

The `remove_transaction` RPC allows any RPC caller to explicitly remove a transaction (and all its descendants) from the tx-pool. However, the underlying `remove_tx` function in `tx-pool/src/pool.rs` silently drops entries without invoking the `call_reject` callback, meaning no `rejected_transaction` event is published to WebSocket subscribers. Off-chain clients (wallets, explorers, DApps) that track transaction lifecycle via the subscription API are left with a stale, incorrect view of the pool.

### Finding Description

CKB's tx-pool uses a `Callbacks` struct with three hooks — `call_pending`, `call_proposed`, and `call_reject` — wired at startup in `shared/src/shared_builder.rs` to publish events through `NotifyController`. Every other removal path in the pool correctly fires `call_reject`:

- `remove_expired` (expiry eviction) — calls `callbacks.call_reject` [1](#0-0) 
- `limit_size` (capacity eviction) — calls `callbacks.call_reject` [2](#0-1) 
- `remove_committed_tx` (conflict on commit) — calls `callbacks.call_reject` [3](#0-2) 
- RBF replacement — calls `self.callbacks.call_reject` [4](#0-3) 

But `remove_tx`, the function backing the `remove_transaction` RPC, does not:

```rust
pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
    let entries = self.pool_map.remove_entry_and_descendants(id);
    !entries.is_empty()
}
``` [5](#0-4) 

The call chain is:

1. RPC handler `remove_transaction` → `tx_pool.remove_local_tx(tx_hash.into())` [6](#0-5) 
2. `Message::RemoveLocalTx` dispatches to `service.remove_tx(tx_hash).await` [7](#0-6) 
3. `TxPoolService::remove_tx` removes from verify queue, orphan pool, or main pool — none of these paths invoke any callback [8](#0-7) 

The `call_reject` callback, when fired, publishes a `rejected_transaction` notification to all WebSocket subscribers via `NotifyController::notify_reject_transaction`: [9](#0-8) 

The subscription system exposes `rejected_transaction` as a first-class topic that clients are explicitly documented to rely on for pool lifecycle tracking: [10](#0-9) 

### Impact Explanation

Any off-chain client — wallet, explorer, DApp — subscribed to `rejected_transaction` events will not receive a notification when a transaction is explicitly removed via `remove_transaction`. The client's internal state diverges from the node's actual pool state: it continues to believe the transaction is pending. This breaks transaction lifecycle tracking, prevents re-submission logic from triggering, and causes explorers to display incorrect pool membership. The gap is permanent until the client polls the pool directly.

### Likelihood Explanation

The `remove_transaction` RPC is a standard, documented, supported endpoint callable by any local RPC user or operator. It is used in integration tests and is part of the normal operational workflow for clearing stuck or invalid transactions. Any deployment that has WebSocket subscribers and uses `remove_transaction` will silently lose the notification.

### Recommendation

In `tx-pool/src/pool.rs`, `remove_tx` should iterate over the removed entries and call `callbacks.call_reject` for each one, analogous to `remove_expired` and `limit_size`. A new `Reject` variant (e.g., `Reject::Removed`) should be introduced to distinguish explicit operator removal from other rejection reasons. The `TxPoolService::remove_tx` in `tx-pool/src/process.rs` must be updated to accept and thread the `callbacks` reference so the notification fires at the service layer.

### Proof of Concept

1. Start a CKB node and open a WebSocket connection subscribed to `rejected_transaction`.
2. Submit a transaction via `send_transaction` — observe the `new_transaction` event fires correctly.
3. Call `remove_transaction` with the same tx hash.
4. Observe: **no `rejected_transaction` event is received** on the WebSocket, even though the transaction has been permanently removed from the pool.
5. Contrast: call `send_transaction` with a duplicate tx (triggering `Reject::Duplicated`) — the `rejected_transaction` event fires immediately, confirming the callback path works for all other rejection reasons.

The root cause is the missing `callbacks.call_reject` invocation in `tx-pool/src/pool.rs:remove_tx` (line 358–361), which is the only removal path in the pool that does not notify subscribers.

### Citations

**File:** tx-pool/src/pool.rs (L259-266)
```rust
            for (entry, reject) in self.pool_map.resolve_conflict(tx) {
                debug!(
                    "removed {} for committed: {}",
                    entry.transaction().hash(),
                    tx.hash()
                );
                callbacks.call_reject(self, &entry, reject);
            }
```

**File:** tx-pool/src/pool.rs (L281-287)
```rust
        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
```

**File:** tx-pool/src/pool.rs (L307-323)
```rust
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
```

**File:** tx-pool/src/pool.rs (L358-361)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
    }
```

**File:** tx-pool/src/process.rs (L219-231)
```rust
        for old in all_removed {
            debug!(
                "remove conflict tx {} for RBF by new tx {}",
                old.transaction().hash(),
                entry.transaction().hash()
            );
            let reject =
                Reject::RBFRejected(format!("replaced by tx {}", entry.transaction().hash()));

            // RBF replace successfully, put old transactions into conflicts pool
            tx_pool.record_conflict(old.transaction().clone());
            // after removing old tx from tx_pool, we call reject callbacks manually
            self.callbacks.call_reject(tx_pool, &old, reject);
```

**File:** tx-pool/src/process.rs (L440-456)
```rust
    pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
        let id = ProposalShortId::from_tx_hash(&tx_hash);
        {
            let mut queue = self.verify_queue.write().await;
            if queue.remove_tx(&id).is_some() {
                return true;
            }
        }
        {
            let mut orphan = self.orphan.write().await;
            if orphan.remove_orphan_tx(&id).is_some() {
                return true;
            }
        }
        let mut tx_pool = self.tx_pool.write().await;
        tx_pool.remove_tx(&id)
    }
```

**File:** rpc/src/module/pool.rs (L662-669)
```rust
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();

        tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
            error!("Send remove_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })
    }
```

**File:** tx-pool/src/service.rs (L826-834)
```rust
        Message::RemoveLocalTx(Request {
            responder,
            arguments: tx_hash,
        }) => {
            let result = service.remove_tx(tx_hash).await;
            if let Err(e) = responder.send(result) {
                error!("Responder sending remove_tx result failed {:?}", e);
            };
        }
```

**File:** shared/src/shared_builder.rs (L576-601)
```rust
    tx_pool_builder.register_reject(Box::new(
        move |tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject| {
            let tx_hash = entry.transaction().hash();
            // record recent reject
            if reject.should_recorded()
                && let Some(ref mut recent_reject) = tx_pool.recent_reject
                && let Err(e) = recent_reject.put(&tx_hash, reject.clone())
            {
                error!("record recent_reject failed {} {} {}", tx_hash, reject, e);
            }

            if reject.is_allowed_relay()
                && let Err(e) = tx_relay_sender.send(TxVerificationResult::Reject {
                    tx_hash: tx_hash.clone(),
                })
            {
                error!("tx-pool tx_relay_sender internal error {}", e);
            }

            // notify
            let notify_tx_entry = create_notify_entry(entry);
            notify_reject.notify_reject_transaction(notify_tx_entry, reject);

            // fee estimator
            fee_estimator.reject_tx(&tx_hash);
        },
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
