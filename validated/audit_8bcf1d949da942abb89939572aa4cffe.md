### Title
`remove_tx` Bypasses `reject_transaction` Notification and `recent_reject` Recording — (`File: tx-pool/src/process.rs`)

### Summary

The CKB tx-pool exposes two distinct code paths that remove a transaction from the pool. The normal rejection path (RBF eviction, capacity eviction, etc.) calls `callbacks.call_reject()`, which fires the `reject_transaction` notification to all subscribers and records the rejection in the `recent_reject` database. The `remove_tx` path — reachable by any local RPC caller via `remove_local_tx` / `remove_transaction` — silently removes the transaction without invoking any callback, without sending a `reject_transaction` notification, and without writing to `recent_reject`. This is a direct analog of [M02]: a second code path achieves the same state change while bypassing the event system that observers depend on.

---

### Finding Description

**Normal rejection path** (RBF, eviction, etc.) in `tx-pool/src/process.rs`:

- `process_rbf` (line 231) calls `self.callbacks.call_reject(tx_pool, &old, reject)` for every RBF-replaced transaction.
- `_submit_entry` (line 146) calls `self.callbacks.call_reject(tx_pool, &evict, reject)` for every capacity-evicted transaction.
- `after_process` (line 523) calls `self.put_recent_reject(&tx_hash, reject)` to record the rejection in the `recent_reject` RocksDB column.

These callbacks ultimately invoke `notify_reject_transaction` on the `NotifyController`, which dispatches a `(PoolTransactionEntry, Reject)` message to every subscriber registered via `subscribe_reject_transaction`.

**`remove_tx` path** in `tx-pool/src/process.rs` (lines 440–456):

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

No callback is invoked. No `reject_transaction` notification is sent. No `recent_reject` entry is written. The transaction simply disappears from the pool.

This function is invoked by `Message::RemoveLocalTx` in `tx-pool/src/service.rs` (lines 826–834), which is dispatched by `TxPoolController::remove_local_tx` (lines 272–275), which is exposed as the `remove_transaction` JSON-RPC endpoint.

---

### Impact Explanation

1. **Subscription subscribers miss the removal.** Any process that has called `subscribe_reject_transaction` (e.g., an external indexer, a wallet backend, a monitoring daemon) will never receive a notification for the removed transaction. From the subscriber's perspective the transaction is still pending, causing a permanently stale view of pool state.

2. **`get_tx_status` / `get_transaction_with_status` return `Unknown` instead of `Rejected`.** Because `recent_reject` is not updated, a subsequent RPC query for the removed transaction returns `TxStatus::Unknown` rather than `TxStatus::Rejected`. Callers that rely on this status to decide whether to rebroadcast or retry a transaction will behave incorrectly.

3. **`clear_pool`** (lines 916–930 of `process.rs`) has the same defect at larger scale: it drops every pending and proposed transaction without firing any `reject_transaction` notification, leaving all subscribers with a completely stale view after a pool reset.

---

### Likelihood Explanation

The `remove_transaction` RPC is a supported, documented local RPC call. Any local RPC user (node operator, wallet software, automated script) can call it. The notification gap is triggered unconditionally every time the call succeeds. No special privilege, key, or race condition is required.

---

### Recommendation

In `remove_tx`, after successfully removing a transaction from the pool map, construct a synthetic `Reject` reason (e.g., `Reject::Expiry` or a new `Reject::Removed` variant) and call `self.callbacks.call_reject(...)` and `self.put_recent_reject(...)` so that:
- All `reject_transaction` subscribers receive the event.
- `get_tx_status` returns `Rejected` rather than `Unknown`.

Apply the same fix to `clear_pool` for every transaction it drops.

---

### Proof of Concept

**Normal rejection (RBF) — notification IS sent:** [1](#0-0) 

**`remove_tx` — notification is NOT sent:** [2](#0-1) 

**RPC entry point dispatching `remove_tx`:** [3](#0-2) 

**Controller method exposed to RPC callers:** [4](#0-3) 

**`reject_transaction` notification channel that `remove_tx` never reaches:** [5](#0-4) 

**`recent_reject` write that `remove_tx` skips:** [6](#0-5)

### Citations

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

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
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

**File:** notify/src/lib.rs (L118-119)
```rust
    reject_transaction_register: NotifyRegister<(PoolTransactionEntry, Reject)>,
    reject_transaction_notifier: Sender<(PoolTransactionEntry, Reject)>,
```
