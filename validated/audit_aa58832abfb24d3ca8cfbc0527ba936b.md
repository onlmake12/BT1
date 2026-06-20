### Title
Missing Callback Emission on Successful Pending→Gap Transition During Reorg — (`tx-pool/src/process.rs`)

### Summary
In `_update_tx_pool_for_reorg`, when a pending transaction is successfully promoted to `Gap` status via `gap_rtx`, no callback is fired. The symmetric `proposed_rtx` path in the same function correctly calls `callbacks.call_proposed(&entry)` on success. This asymmetry means RPC subscribers to `new_transaction` or `proposed_transaction` receive no notification when a transaction enters the gap window during a chain reorg, breaking the observable transaction lifecycle for any external tool or RPC subscriber.

### Finding Description

The CKB tx-pool has three transaction statuses: `Pending`, `Gap`, and `Proposed`. [1](#0-0) 

The `Callbacks` struct holds three hooks — `pending`, `proposed`, and `reject` — wired at startup to fire `notify_new_transaction`, `notify_proposed_transaction`, and `notify_reject_transaction` respectively. [2](#0-1) [3](#0-2) 

When a transaction is **first submitted** to the pool with `TxStatus::Gap`, `_submit_entry` correctly calls `callbacks.call_pending(&entry)`, firing the `new_transaction` notification: [4](#0-3) 

During a reorg, `_update_tx_pool_for_reorg` promotes existing pool entries. For the **proposed** path, `callbacks.call_proposed(&entry)` is called on success: [5](#0-4) 

For the **gap** path, on success **no callback is called at all**: [6](#0-5) 

Only `callbacks.call_reject` is invoked on failure. A successful `Pending → Gap` state change during reorg is entirely silent to all subscribers.

### Impact Explanation

Any RPC client subscribed to `new_transaction` or `proposed_transaction` topics will not receive a notification when a pending transaction transitions to `Gap` during a reorg. The `gap` status is a real, externally observable pool state (visible via `get_pool_entry` RPC and documented in `PoolTxDetailInfo`): [7](#0-6) 

Wallets, block explorers, and monitoring tools that rely on subscription notifications to track the full transaction lifecycle (`pending → gap → proposed → committed`) will silently miss the `gap` transition during reorgs, producing an inconsistent view of pool state. This is the direct analog of the ERC865 `transferFromPreSigned` missing its `TransferPreSigned` event.

### Likelihood Explanation

Every block attachment in mine mode triggers `_update_tx_pool_for_reorg`. Any transaction whose proposal short ID falls in the gap window of the newly attached block will undergo this silent transition. This is a normal, frequent code path reachable by any block relayer or miner submitting a valid block.

### Recommendation

Add a `call_pending` (or a dedicated `call_gap`) callback invocation on the success branch of the gap loop in `_update_tx_pool_for_reorg`, mirroring the `call_proposed` call in the proposals loop:

```rust
for (id, entry) in gaps {
    debug!("begin to gap: {:x}", id);
    if let Err(e) = tx_pool.gap_rtx(&id) {
        // ...
        callbacks.call_reject(tx_pool, &entry, e.clone());
    } else {
        callbacks.call_pending(&entry)  // <-- add this
    }
}
```

Alternatively, introduce a dedicated `GapCallback` type in `Callbacks` and a corresponding `notify_gap_transaction` channel in `NotifyController`, consistent with how `proposed_transaction` is handled. [8](#0-7) 

### Proof of Concept

1. Start a CKB node in mine mode.
2. Subscribe to `new_transaction` and `proposed_transaction` via WebSocket RPC.
3. Submit a transaction to the pool (it enters `Pending`; `new_transaction` fires — correct).
4. Mine a block that includes the transaction's proposal short ID in the gap window.
5. `_update_tx_pool_for_reorg` runs; `gap_rtx` succeeds; **no notification fires**.
6. Mine another block that moves the transaction to `Proposed`; `proposed_transaction` fires — correct.

The subscriber observes the transaction jump from `Pending` directly to `Proposed` with no intermediate `Gap` notification, despite the pool internally recording the `Gap` state transition. [6](#0-5) [9](#0-8)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L23-28)
```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Status {
    Pending,
    Gap,
    Proposed,
}
```

**File:** tx-pool/src/callback.rs (L1-17)
```rust
use super::component::TxEntry;
use crate::error::Reject;
use crate::pool::TxPool;

/// Callback boxed fn pointer wrapper
pub type PendingCallback = Box<dyn Fn(&TxEntry) + Sync + Send>;
/// Proposed Callback boxed fn pointer wrapper
pub type ProposedCallback = Box<dyn Fn(&TxEntry) + Sync + Send>;
/// Reject Callback boxed fn pointer wrapper
pub type RejectCallback = Box<dyn Fn(&mut TxPool, &TxEntry, Reject) + Sync + Send>;

/// Struct hold callbacks
pub struct Callbacks {
    pub(crate) pending: Option<PendingCallback>,
    pub(crate) proposed: Option<ProposedCallback>,
    pub(crate) reject: Option<RejectCallback>,
}
```

**File:** shared/src/shared_builder.rs (L559-573)
```rust
    tx_pool_builder.register_pending(Box::new(move |entry: &TxEntry| {
        // notify
        let notify_tx_entry = create_notify_entry(entry);
        notify_pending.notify_new_transaction(notify_tx_entry);
        let tx_hash = entry.transaction().hash();
        let entry_info = entry.to_info();
        fee_estimator_clone.accept_tx(tx_hash, entry_info);
    }));

    let notify_proposed = notify.clone();
    tx_pool_builder.register_proposed(Box::new(move |entry: &TxEntry| {
        // notify
        let notify_tx_entry = create_notify_entry(entry);
        notify_proposed.notify_proposed_transaction(notify_tx_entry);
    }));
```

**File:** tx-pool/src/process.rs (L1029-1034)
```rust
    if succ {
        match status {
            TxStatus::Fresh => callbacks.call_pending(&entry),
            TxStatus::Gap => callbacks.call_pending(&entry),
            TxStatus::Proposed => callbacks.call_proposed(&entry),
        }
```

**File:** tx-pool/src/process.rs (L1082-1093)
```rust
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
```

**File:** tx-pool/src/process.rs (L1096-1106)
```rust
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
```

**File:** util/types/src/core/tx_pool.rs (L383-384)
```rust
    /// The detailed status in tx-pool, `Pending`, `Gap`, `Proposed`
    pub entry_status: String,
```

**File:** notify/src/lib.rs (L504-512)
```rust
    /// Notifies all subscribers of a new transaction in the transaction pool.
    pub fn notify_new_transaction(&self, tx_entry: PoolTransactionEntry) {
        let new_transaction_notifier = self.new_transaction_notifier.clone();
        self.handle.spawn(async move {
            if let Err(e) = new_transaction_notifier.send(tx_entry).await {
                error!("notify_new_transaction channel is closed: {}", e);
            }
        });
    }
```
