### Title
Missing Chain-Reorganization (Detached-Block) Notification in Subscription API — (`notify/src/lib.rs`, `chain/src/verify.rs`, `rpc/src/module/subscription.rs`)

### Summary

The CKB node's pub/sub notification system (`NotifyController`) emits a `new_tip_block` event only for the **newly attached tip block** when a chain reorganization occurs. It never emits any notification for the **detached blocks** that were removed from the canonical chain. Off-chain services (wallets, exchanges, indexers) that subscribe to `new_tip_block` / `new_tip_header` via the RPC subscription API cannot distinguish a normal block append from a reorg, and cannot learn which previously-confirmed transactions have been rolled back. The subscription stream is therefore insufficient to reconstruct the canonical chain state, directly analogous to the Ribbon-v2 finding that events must be complete enough to rebuild contract state.

### Finding Description

When a new best block is accepted, `chain/src/verify.rs` calls `find_fork` and `rollback` to compute `ForkChanges`, which contains both `attached_blocks` and `detached_blocks`. [1](#0-0) 

The detached blocks are forwarded to the tx-pool for internal pool reconciliation: [2](#0-1) 

But the external notification call that follows only publishes the **new tip block**; the detached blocks are never passed to `NotifyController`: [3](#0-2) 

`NotifyController` has no channel, no subscriber map, and no API for detached-block or reorg events: [4](#0-3) 

Consequently, the RPC subscription module, which bridges `NotifyController` to WebSocket/TCP clients, exposes no reorg topic: [5](#0-4) 

The documented subscription topics are `new_tip_header`, `new_tip_block`, `new_transaction`, `proposed_transaction`, `rejected_transaction`, and `logs` — none of which carries detached-block information: [6](#0-5) 

### Impact Explanation

Any off-chain service (exchange, wallet, indexer) that uses the CKB subscription API to track confirmed transactions will:

1. Receive a `new_tip_block` notification for the reorg tip.
2. Receive **no** notification for the detached blocks.
3. Have no way to know that transactions previously seen in detached blocks are now unconfirmed.

A service that credits user balances or marks withdrawals as final upon seeing a transaction in a `new_tip_block` notification cannot detect that the block was later rolled back. This enables a double-spend attack: broadcast a transaction, wait for it to appear in a block (triggering the `new_tip_block` notification to the victim service), then mine a longer competing chain that excludes the transaction. The victim service never receives a rollback signal.

The `ForkChanges` struct already tracks all detached blocks internally; the gap is purely in the notification path.

### Likelihood Explanation

Chain reorganizations are a routine network event — any block relayer or miner (unprivileged peer) can trigger one by propagating a competing chain of equal or greater total difficulty. No special privilege, key, or majority hash power is required for short reorgs (1–2 blocks), which are the most common and the most dangerous for services that confirm after a small number of blocks. The subscription API is the primary real-time integration point for off-chain services, making this omission practically reachable.

### Recommendation

1. Add a `detached_block` (or `chain_reorg`) notification channel to `NotifyController` in `notify/src/lib.rs`, carrying both the list of detached blocks and the list of attached blocks.
2. In `chain/src/verify.rs`, after computing `fork`, call `notify_controller.notify_chain_reorg(fork.detached_blocks().clone(), fork.attached_blocks().clone())` alongside the existing `notify_new_block` call.
3. Expose a new subscription topic (e.g., `chain_reorg`) in `rpc/src/module/subscription.rs` so that WebSocket/TCP subscribers receive the full reorg delta.
4. Document that the subscription stream is now sufficient to reconstruct canonical chain state without additional polling.

### Proof of Concept

1. Subscribe to `new_tip_block` via WebSocket.
2. Submit transaction T spending cell C; observe T appear in block B₁ (notification received).
3. Mine a competing chain of length 2 that does not include T, starting from B₁'s parent.
4. Relay the competing chain to the node; the node reorgs, detaches B₁, attaches B₂ and B₃.
5. Observe: the subscriber receives `new_tip_block` for B₃ only. No notification is received for the detachment of B₁. The subscriber's view of T as "confirmed" is never corrected.
6. T is now back in the mempool (or dropped), but the off-chain service has no signal to reverse its credit.

The code path is:

```
block relayer submits B₂/B₃
  → chain/src/verify.rs: find_fork() populates fork.detached_blocks = [B₁]
  → tx_pool_controller.update_tx_pool_for_reorg(detached=[B₁], attached=[B₂,B₃], ...)
  → notify_controller.notify_new_block(B₃)   // ← only this fires externally
  // fork.detached_blocks is never sent to NotifyController
``` [7](#0-6) [8](#0-7)

### Citations

**File:** chain/src/utils/forkchanges.rs (L9-18)
```rust
pub struct ForkChanges {
    /// Blocks attached to index after forks
    pub(crate) attached_blocks: VecDeque<BlockView>,
    /// Blocks detached from index after forks
    pub(crate) detached_blocks: VecDeque<BlockView>,
    /// HashSet with proposal_id detached to index after forks
    pub(crate) detached_proposal_id: HashSet<ProposalShortId>,
    /// to be updated exts
    pub(crate) dirty_exts: VecDeque<BlockExt>,
}
```

**File:** chain/src/verify.rs (L386-408)
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

            self.shared
                .notify_controller()
                .notify_new_block(block.to_owned());
            if log_enabled!(ckb_logger::Level::Trace) {
                self.print_chain(10);
            }
            if let Some(metrics) = ckb_metrics::handle() {
                metrics.ckb_chain_tip.set(block.header().number() as i64);
            }
```

**File:** notify/src/lib.rs (L110-125)
```rust
pub struct NotifyController {
    new_block_register: NotifyRegister<BlockView>,
    new_block_watcher: NotifyWatcher<Byte32>,
    new_block_notifier: Sender<BlockView>,
    new_transaction_register: NotifyRegister<PoolTransactionEntry>,
    new_transaction_notifier: Sender<PoolTransactionEntry>,
    proposed_transaction_register: NotifyRegister<PoolTransactionEntry>,
    proposed_transaction_notifier: Sender<PoolTransactionEntry>,
    reject_transaction_register: NotifyRegister<(PoolTransactionEntry, Reject)>,
    reject_transaction_notifier: Sender<(PoolTransactionEntry, Reject)>,
    network_alert_register: NotifyRegister<Alert>,
    network_alert_notifier: Sender<Alert>,
    log_register: NotifyRegister<LogEntry>,
    log_notifier: Sender<LogEntry>,
    handle: Handle,
}
```

**File:** notify/src/lib.rs (L482-490)
```rust
    /// Notifies all subscribers of a new block.
    pub fn notify_new_block(&self, block: BlockView) {
        let new_block_notifier = self.new_block_notifier.clone();
        self.handle.spawn(async move {
            if let Err(e) = new_block_notifier.send(block).await {
                error!("notify_new_block channel is closed: {}", e);
            }
        });
    }
```

**File:** rpc/src/module/subscription.rs (L193-201)
```rust
#[derive(Clone)]
pub struct SubscriptionRpcImpl {
    pub new_tip_header_sender: broadcast::Sender<PublishMsg<String>>,
    pub new_tip_block_sender: broadcast::Sender<PublishMsg<String>>,
    pub new_transaction_sender: broadcast::Sender<PublishMsg<String>>,
    pub proposed_transaction_sender: broadcast::Sender<PublishMsg<String>>,
    pub new_reject_transaction_sender: broadcast::Sender<PublishMsg<String>>,
    pub log_sender: broadcast::Sender<PublishMsg<String>>,
}
```

**File:** rpc/src/module/subscription.rs (L214-222)
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
```
