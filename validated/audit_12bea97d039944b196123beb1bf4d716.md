### Title
Missing Chain-Reorganization (Detached-Block) Event in RPC Subscription System — (`chain/src/verify.rs`, `notify/src/lib.rs`, `util/jsonrpc-types/src/subscription.rs`)

---

### Summary

The CKB RPC subscription system emits a `new_tip_block` / `new_tip_header` push message every time the canonical chain tip advances, but it never emits a corresponding event for blocks that are **detached** (rolled back) during a chain reorganization. The `ForkChanges::detached_blocks` computed inside `ConsumeUnverifiedBlockProcessor::verify_block` is silently discarded after the internal tx-pool update; it is never forwarded to the `NotifyController` and therefore never reaches any RPC subscriber. Off-chain consumers that rely on the subscription stream to track cell state have no way to detect that previously "confirmed" transactions have been invalidated by a reorg.

---

### Finding Description

When a new best block is accepted, `chain/src/verify.rs` executes the following sequence:

1. `find_fork` populates a `ForkChanges` value that contains both `attached_blocks` (the new fork) and `detached_blocks` (the old fork being rolled back). [1](#0-0) 

2. `rollback` removes the detached blocks from the database. [2](#0-1) 

3. `update_tx_pool_for_reorg` is called with both `fork.detached_blocks()` and `fork.attached_blocks()` so the tx-pool can re-queue or evict transactions. [3](#0-2) 

4. **Only the new tip block is passed to `notify_new_block`.** The `fork` object — and its `detached_blocks` — is never forwarded to the `NotifyController`. [4](#0-3) 

The `NotifyController` struct has no channel for detached-block events at all: [5](#0-4) 

The `NotifyService` event loop handles only: `new_block`, `new_transaction`, `proposed_transaction`, `reject_transaction`, `network_alert`, and `log`. There is no `detached_block` arm. [6](#0-5) 

The RPC `Topic` enum exposed to subscribers likewise has no `DetachedBlock` or `ChainReorg` variant: [7](#0-6) 

The subscription implementation in `SubscriptionRpcImpl::new` bridges the internal `NotifyController` channels to broadcast senders for each topic. Because there is no detached-block channel, no such bridge exists and no reorg information ever reaches a WebSocket/TCP subscriber. [8](#0-7) 

---

### Impact Explanation

An unprivileged RPC caller that subscribes to `new_tip_block` or `new_tip_header` receives a stream of canonical-tip blocks. During a reorg, the subscriber sees the new tip block but receives **no signal** that one or more previously delivered blocks have been detached. From the subscriber's perspective, the stream looks identical whether the new block is a simple extension or the result of a deep reorg.

Consequences for off-chain consumers:

- **Indexers** that apply cell-state deltas from each `new_tip_block` message will silently accumulate incorrect state: cells created in detached blocks remain marked as live, and cells consumed in detached blocks remain marked as spent.
- **Dashboards and wallets** will display incorrect balances and transaction histories until they independently detect the reorg by polling `get_block_by_number` and comparing hashes.
- **Exchange deposit monitors** that credit deposits upon receiving a `new_tip_block` containing a deposit transaction have no subscription-level signal that the block was later detached, creating a window for double-spend exploitation at the application layer.

The only current mitigation is for each subscriber to implement its own polling-based reorg detection (comparing the `parent_hash` of each new block against the previously seen tip hash), which is exactly the "brittle dependency on internal transaction formats" described in the reference report.

---

### Likelihood Explanation

Chain reorganizations are a routine occurrence on any PoW network. On CKB mainnet, short reorgs (1–2 blocks) happen whenever two miners find valid blocks at nearly the same time. The `ForkChanges` code path is exercised on every such event. Any indexer or off-chain consumer that subscribes to `new_tip_block` without implementing independent reorg detection is affected every time a reorg occurs. The entry path requires only a standard WebSocket or TCP RPC connection — no special privileges, no key material, no majority hashpower.

---

### Recommendation

1. Add a `detached_blocks` (or `chain_reorg`) notification channel to `NotifyController` in `notify/src/lib.rs`, carrying the `VecDeque<BlockView>` of detached blocks alongside the new tip.
2. In `chain/src/verify.rs`, after `rollback` and before (or alongside) `notify_new_block`, call a new `notify_detached_blocks(&fork.detached_blocks)` when `fork.has_detached()` is true.
3. Expose a new `Topic::DetachedBlock` (or `Topic::ChainReorg`) variant in `util/jsonrpc-types/src/subscription.rs` and wire it through `SubscriptionRpcImpl` so WebSocket/TCP subscribers can receive detached-block push messages.
4. Document the reorg-detection contract in the RPC README so consumers know they must handle both `new_tip_block` and `detached_block` events to maintain a consistent cell-state view.

---

### Proof of Concept

**Subscription stream during a reorg (current behavior):**

```
// Subscriber connects and receives:
{ "method": "subscribe", "params": { "result": <BlockView N>,   "subscription": "0x1" } }
{ "method": "subscribe", "params": { "result": <BlockView N+1>, "subscription": "0x1" } }
// Reorg occurs: blocks N and N+1 are detached; blocks N' and N+1' are attached
{ "method": "subscribe", "params": { "result": <BlockView N+1'>, "subscription": "0x1" } }
// Subscriber has NO indication that N and N+1 were rolled back.
// Its cell-state index is now silently wrong.
```

**Code trace confirming the gap:**

```
chain/src/verify.rs::verify_block()
  └─ find_fork(&mut fork, ...)          // fork.detached_blocks = [N, N+1]
  └─ rollback(&fork, &db_txn)           // DB updated; detached blocks removed from index
  └─ update_tx_pool_for_reorg(          // tx-pool sees detached_blocks
       fork.detached_blocks(), ...)
  └─ notify_controller()
       .notify_new_block(block)         // ← only new tip; fork.detached_blocks DROPPED HERE
```

The `notify_new_block` call at `chain/src/verify.rs:400–402` accepts only a single `BlockView` (the new tip). The `ForkChanges` struct — which holds `detached_blocks` — goes out of scope immediately after without being forwarded to any subscriber channel. [4](#0-3) [9](#0-8)

### Citations

**File:** chain/src/verify.rs (L340-341)
```rust
            self.find_fork(&mut fork, current_tip_header.number(), block, ext);
            self.rollback(&fork, &db_txn)?;
```

**File:** chain/src/verify.rs (L387-398)
```rust
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

**File:** chain/src/verify.rs (L481-486)
```rust
    pub(crate) fn rollback(&self, fork: &ForkChanges, txn: &StoreTransaction) -> Result<(), Error> {
        for block in fork.detached_blocks().iter().rev() {
            txn.detach_block(block)?;
            detach_block_cell(txn, block)?;
        }
        Ok(())
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

**File:** notify/src/lib.rs (L196-219)
```rust
        handle.spawn(async move {
            loop {
                tokio::select! {
                    Some(msg) = new_block_register_receiver.recv() => { self.handle_register_new_block(msg) },
                    Some(msg) = new_block_watcher_receiver.recv() => { self.handle_watch_new_block(msg) },
                    Some(msg) = new_block_receiver.recv() => { self.handle_notify_new_block(msg) },
                    Some(msg) = new_transaction_register_receiver.recv() => { self.handle_register_new_transaction(msg) },
                    Some(msg) = new_transaction_receiver.recv() => { self.handle_notify_new_transaction(msg) },
                    Some(msg) = proposed_transaction_register_receiver.recv() => { self.handle_register_proposed_transaction(msg) },
                    Some(msg) = proposed_transaction_receiver.recv() => { self.handle_notify_proposed_transaction(msg) },
                    Some(msg) = reject_transaction_register_receiver.recv() => { self.handle_register_reject_transaction(msg) },
                    Some(msg) = reject_transaction_receiver.recv() => { self.handle_notify_reject_transaction(msg) },
                    Some(msg) = network_alert_register_receiver.recv() => { self.handle_register_network_alert(msg) },
                    Some(msg) = network_alert_receiver.recv() => { self.handle_notify_network_alert(msg) },
                    Some(msg) = log_register_receiver.recv() => { self.handle_register_log(msg) },
                    Some(msg) = log_receiver.recv() => { self.handle_notify_log(msg) },
                    _ = stop_token_clone.cancelled() => {
                        info!("NotifyService received exit signal, exit now");
                        break;
                    }
                    else => break,
                }
            }
        });
```

**File:** notify/src/lib.rs (L483-490)
```rust
    pub fn notify_new_block(&self, block: BlockView) {
        let new_block_notifier = self.new_block_notifier.clone();
        self.handle.spawn(async move {
            if let Err(e) = new_block_notifier.send(block).await {
                error!("notify_new_block channel is closed: {}", e);
            }
        });
    }
```

**File:** util/jsonrpc-types/src/subscription.rs (L6-19)
```rust
pub enum Topic {
    /// Subscribe new tip headers.
    NewTipHeader,
    /// Subscribe new tip blocks.
    NewTipBlock,
    /// Subscribe new transactions which are submitted to the pool.
    NewTransaction,
    /// Subscribe in-pool transactions which proposed on chain.
    ProposedTransaction,
    /// Subscribe transactions which are abandoned by tx-pool.
    RejectedTransaction,
    /// Subscribe to logs.
    Log,
}
```

**File:** rpc/src/module/subscription.rs (L259-331)
```rust
impl SubscriptionRpcImpl {
    pub fn new(notify_controller: NotifyController, handle: Handle) -> Self {
        const SUBSCRIBER_NAME: &str = "TcpSubscription";

        let mut new_block_receiver =
            handle.block_on(notify_controller.subscribe_new_block(SUBSCRIBER_NAME.to_string()));
        let mut new_transaction_receiver = handle
            .block_on(notify_controller.subscribe_new_transaction(SUBSCRIBER_NAME.to_string()));
        let mut proposed_transaction_receiver = handle.block_on(
            notify_controller.subscribe_proposed_transaction(SUBSCRIBER_NAME.to_string()),
        );
        let mut reject_transaction_receiver = handle
            .block_on(notify_controller.subscribe_reject_transaction(SUBSCRIBER_NAME.to_string()));
        let mut log_receiver =
            handle.block_on(notify_controller.subscribe_log(SUBSCRIBER_NAME.to_string()));

        let (new_tip_header_sender, _) = broadcast::channel(NOTIFY_CHANNEL_SIZE);
        let (new_tip_block_sender, _) = broadcast::channel(NOTIFY_CHANNEL_SIZE);
        let (proposed_transaction_sender, _) = broadcast::channel(NOTIFY_CHANNEL_SIZE);
        let (new_transaction_sender, _) = broadcast::channel(NOTIFY_CHANNEL_SIZE);
        let (new_reject_transaction_sender, _) = broadcast::channel(NOTIFY_CHANNEL_SIZE);
        let (log_sender, _) = broadcast::channel(NOTIFY_CHANNEL_SIZE);

        let stop_rx = new_tokio_exit_rx();
        handle.spawn({
            let new_tip_header_sender = new_tip_header_sender.clone();
            let new_tip_block_sender = new_tip_block_sender.clone();
            let new_transaction_sender = new_transaction_sender.clone();
            let proposed_transaction_sender = proposed_transaction_sender.clone();
            let new_reject_transaction_sender = new_reject_transaction_sender.clone();
            let log_sender = log_sender.clone();
            async move {
                loop {
                    tokio::select! {
                        Some(block) = new_block_receiver.recv() => {
                            publiser_send!(ckb_jsonrpc_types::HeaderView, block.header(), new_tip_header_sender);
                            publiser_send!(ckb_jsonrpc_types::BlockView, block, new_tip_block_sender);
                        },
                        Some(tx_entry) = new_transaction_receiver.recv() => {
                            publiser_send!(ckb_jsonrpc_types::PoolTransactionEntry, tx_entry, new_transaction_sender);
                        },
                        Some(tx_entry) = proposed_transaction_receiver.recv() => {
                            publiser_send!(ckb_jsonrpc_types::PoolTransactionEntry, tx_entry, proposed_transaction_sender);
                        },
                        Some((tx_entry, reject)) = reject_transaction_receiver.recv() => {
                            publiser_send!((ckb_jsonrpc_types::PoolTransactionEntry, ckb_jsonrpc_types::PoolTransactionReject),
                                            (tx_entry.into(), reject.into()),
                                            new_reject_transaction_sender);
                        },
                        Some(log_entry) = log_receiver.recv() => {
                            publiser_send!(ckb_jsonrpc_types::LogEntry, convert_log_entry(log_entry), log_sender);
                        },
                        _ = stop_rx.cancelled() => {
                            break;
                        },
                        else => {
                            error!("SubscriptionRpcImpl tokio::select! unexpected error");
                            break;
                        }
                    }
                }
            }
        });

        Self {
            new_tip_header_sender,
            new_tip_block_sender,
            new_transaction_sender,
            proposed_transaction_sender,
            new_reject_transaction_sender,
            log_sender,
        }
    }
```
