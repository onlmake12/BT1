### Title
Incorrect Expiry Timestamp in `Reject::Expiry` Notification Sent to `rejected_transaction` Subscribers — (`tx-pool/src/pool.rs`)

### Summary

In `tx-pool/src/pool.rs`, the `remove_expired` function constructs `Reject::Expiry(entry.timestamp)` using the transaction's **pool-entry time** rather than the actual **expiry deadline** (`entry.timestamp + self.expiry`). This incorrect value propagates through the callback chain into the `rejected_transaction` RPC subscription notification, causing every subscriber to receive a wrong timestamp in the rejection reason.

### Finding Description

`remove_expired` in `tx-pool/src/pool.rs` iterates over all pool entries whose age exceeds `self.expiry`, removes them, and fires the reject callback:

```rust
// tx-pool/src/pool.rs  lines 271-287
pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
    let now_ms = ckb_systemtime::unix_time_as_millis();

    let removed: Vec<_> = self
        .pool_map
        .iter()
        .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
        .map(|entry| entry.inner.clone())
        .collect();

    for entry in removed {
        let tx_hash = entry.transaction().hash();
        debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
        self.pool_map.remove_entry(&entry.proposal_short_id());
        let reject = Reject::Expiry(entry.timestamp);   // ← BUG: should be entry.timestamp + self.expiry
        callbacks.call_reject(self, &entry, reject);
    }
}
``` [1](#0-0) 

The `Reject::Expiry` variant is defined as:

```rust
/// Expired
#[error("Expiry transaction, timestamp {0}")]
Expiry(u64),
``` [2](#0-1) 

The error message reads *"Expiry transaction, timestamp {0}"*, clearly intending `{0}` to be the expiry deadline. However, `entry.timestamp` is the Unix millisecond timestamp at which the transaction **entered** the pool, not the time at which it **expires**. The correct expiry deadline is `entry.timestamp + self.expiry`.

The reject callback chain is:

1. `callbacks.call_reject(self, &entry, reject)` — `tx-pool/src/callback.rs`
2. → `register_reject` closure in `shared/src/shared_builder.rs` calls `notify_reject.notify_reject_transaction(notify_tx_entry, reject)` [3](#0-2) 
3. → `NotifyController::notify_reject_transaction` sends `(PoolTransactionEntry, Reject)` to all subscribers [4](#0-3) 
4. → `SubscriptionRpcImpl` serializes and pushes the pair to all `rejected_transaction` WebSocket/TCP subscribers [5](#0-4) 

The `PoolTransactionEntry` passed alongside the reject also carries `entry.timestamp` (pool-entry time), so subscribers receive two timestamps: one in the entry (correct — pool-entry time) and one embedded in the `Reject::Expiry` string (incorrect — also pool-entry time, but labelled as the expiry deadline).

### Impact Explanation

Any process subscribed to the `rejected_transaction` RPC topic (WebSocket or TCP) receives a `PoolTransactionReject` whose `Expiry` reason embeds the wrong timestamp. Off-chain tools — wallets, monitoring dashboards, resubmission bots — that parse this field to determine *when* a transaction expired will compute an incorrect expiry deadline. A tool that uses the expiry timestamp to decide whether to resubmit a transaction may resubmit too early or too late, or may misattribute the cause of rejection.

### Likelihood Explanation

Any unprivileged RPC caller can submit a transaction via `send_transaction`. If the transaction is not confirmed within the pool's expiry window, `remove_expired` fires automatically on the next periodic sweep and emits the incorrect notification. No special privileges, keys, or network position are required. The condition is routinely triggered in normal node operation.

### Recommendation

Replace `entry.timestamp` with the actual expiry deadline in the `Reject::Expiry` constructor:

```rust
// tx-pool/src/pool.rs
let reject = Reject::Expiry(entry.timestamp + self.expiry);
```

This makes the embedded timestamp consistent with the error message's intent ("Expiry transaction, timestamp {0}") and gives subscribers an accurate deadline.

### Proof of Concept

1. Start a CKB node with a short `expiry` window (e.g., 10 seconds for testing).
2. Subscribe to `rejected_transaction` via WebSocket: `{"id":1,"jsonrpc":"2.0","method":"subscribe","params":["rejected_transaction"]}`.
3. Submit a transaction that will not be confirmed: `send_transaction`.
4. Wait for the expiry sweep to fire.
5. Observe the push notification: the `Expiry` reason contains `entry.timestamp` (the submission time), **not** `entry.timestamp + self.expiry` (the actual expiry deadline). The two values differ by exactly `self.expiry` milliseconds, confirming the off-by-expiry error. [1](#0-0) [2](#0-1) [6](#0-5) [4](#0-3) [7](#0-6)

### Citations

**File:** tx-pool/src/pool.rs (L271-287)
```rust
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
```

**File:** util/types/src/core/tx_pool.rs (L56-58)
```rust
    /// Expired
    #[error("Expiry transaction, timestamp {0}")]
    Expiry(u64),
```

**File:** shared/src/shared_builder.rs (L575-602)
```rust
    let notify_reject = notify;
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
    ));
```

**File:** notify/src/lib.rs (L549-555)
```rust
    pub fn notify_reject_transaction(&self, tx_entry: PoolTransactionEntry, reject: Reject) {
        let reject_transaction_notifier = self.reject_transaction_notifier.clone();
        self.handle.spawn(async move {
            if let Err(e) = reject_transaction_notifier.send((tx_entry, reject)).await {
                error!("notify_reject_transaction channel is closed: {}", e);
            }
        });
```

**File:** rpc/src/module/subscription.rs (L303-307)
```rust
                        Some((tx_entry, reject)) = reject_transaction_receiver.recv() => {
                            publiser_send!((ckb_jsonrpc_types::PoolTransactionEntry, ckb_jsonrpc_types::PoolTransactionReject),
                                            (tx_entry.into(), reject.into()),
                                            new_reject_transaction_sender);
                        },
```
