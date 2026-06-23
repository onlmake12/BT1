### Title
Dropped Reorg Notification via `try_send` Leaves Tx-Pool Permanently Inconsistent with Chain State - (`tx-pool/src/service.rs`, `chain/src/verify.rs`)

---

### Summary

After the chain service commits a new best block (including any reorg), it sends a reorg notification to the tx-pool via a **non-blocking** `try_send` on a bounded channel. If the channel is full, the notification is silently dropped and the chain continues. The tx-pool never learns about the reorg, leaving it permanently inconsistent with the committed chain state. There is no retry mechanism.

---

### Finding Description

In `chain/src/verify.rs`, after a block is committed to the database and the snapshot is updated, the chain service calls `tx_pool_controller.update_tx_pool_for_reorg(...)` to notify the tx-pool of detached and attached blocks: [1](#0-0) 

`update_tx_pool_for_reorg` in `tx-pool/src/service.rs` sends the notification via `reorg_sender.try_send(notify)` — a **non-blocking** send on a bounded channel: [2](#0-1) 

The `reorg_sender` channel is created with `DEFAULT_CHANNEL_SIZE = 512`: [3](#0-2) 

The reorg receiver loop processes one reorg at a time, sequentially: [4](#0-3) 

Each reorg processing call (`update_tx_pool_for_reorg`) can be expensive — it re-verifies all detached transactions via `readd_detached_tx`, which runs full CKB-VM script execution: [5](#0-4) 

If the reorg receiver is busy processing a large reorg while the chain service rapidly commits more blocks (e.g., during a deep reorg or rapid block production), the bounded channel fills up. Subsequent `try_send` calls return `TrySendError::Full`. The error is logged in `chain/src/verify.rs` but execution continues — the chain state is already committed and there is no retry: [6](#0-5) 

The tx-pool is now permanently out of sync with the chain. The `SyncState` analog is the `pending_compact_blocks` map, which similarly stores state awaiting async resolution with no guaranteed cleanup path: [7](#0-6) 

---

### Impact Explanation

When a reorg notification is dropped:

1. **Transactions from detached blocks are not re-added to the mempool.** Users who submitted transactions that were in a detached (orphaned) block silently lose their place in the mempool. Their transactions are gone with no error or notification.
2. **Transactions from attached blocks remain in the mempool.** The tx-pool does not call `remove_committed_txs`, so already-committed transactions stay in the pending pool. Miners building block templates will attempt to include already-committed transactions, producing invalid block templates.
3. **The tx-pool snapshot is stale.** All subsequent operations — fee estimation, RBF checks, cell resolution, block assembly — operate on a snapshot that does not reflect the actual chain tip. This can cause valid transactions to be rejected or invalid ones to be accepted.

The inconsistency is **permanent** — there is no self-healing mechanism. The tx-pool will not re-synchronize until the node restarts.

---

### Likelihood Explanation

This is reachable by an unprivileged block relayer or miner. The trigger condition is:

- A deep chain reorganization (e.g., 50+ blocks detached) where each reorg notification takes significant time to process (due to re-verifying many transactions with CKB-VM).
- While the reorg receiver is busy, the chain service continues committing blocks from the new chain, each generating a new `try_send` call.
- With 512 slots and slow reorg processing, the channel fills up within seconds during a deep reorg on a loaded node.

An adversary with moderate hashpower can deliberately trigger deep reorgs to exhaust the channel. Even without adversarial intent, this can occur naturally during IBD catch-up or during a competitive mining period with frequent short reorgs.

---

### Recommendation

Replace `try_send` with a blocking `send` (or `send().await`) for the reorg notification, or implement a retry loop with backpressure. The chain service must not proceed past the reorg notification if the tx-pool cannot be updated — the two components must remain consistent. Alternatively, use an unbounded channel for reorg notifications (since reorgs are rare and bounded in practice by the chain's fork-choice rules), accepting the memory trade-off.

---

### Proof of Concept

1. Start a CKB node with a loaded tx-pool (many pending transactions).
2. Trigger a deep chain reorganization of depth > 512 blocks (or trigger many rapid shallow reorgs while the tx-pool is busy re-verifying detached transactions from the first reorg).
3. Observe that `[verify block] notify update_tx_pool_for_reorg error` is logged in `chain/src/verify.rs` line 393.
4. Query the tx-pool via RPC (`get_pool_tx_details`, `get_transaction`): transactions from detached blocks are absent from the pool, and transactions from attached blocks are still present.
5. Request a block template: the template will contain already-committed transactions, producing an invalid block.

The root cause is the non-blocking `try_send` at `tx-pool/src/service.rs:254` combined with the absence of any retry or fallback in `chain/src/verify.rs:387–394`.

### Citations

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

**File:** tx-pool/src/service.rs (L511-513)
```rust
        let (sender, receiver) = mpsc::channel(DEFAULT_CHANNEL_SIZE);
        let block_assembler_channel = mpsc::channel(BLOCK_ASSEMBLER_CHANNEL_SIZE);
        let (reorg_sender, reorg_receiver) = mpsc::channel(DEFAULT_CHANNEL_SIZE);
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

**File:** tx-pool/src/process.rs (L878-913)
```rust
    async fn readd_detached_tx(
        &self,
        tx_pool: &mut TxPool,
        txs: Vec<TransactionView>,
        fetched_cache: HashMap<Byte32, CacheEntry>,
    ) {
        let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
        for tx in txs {
            let tx_size = tx.data().serialized_size_in_block();
            let tx_hash = tx.hash();
            if let Ok((rtx, status)) = resolve_tx(tx_pool, tx_pool.snapshot(), tx, false)
                && let Ok(fee) = check_tx_fee(tx_pool, tx_pool.snapshot(), &rtx, tx_size)
            {
                let verify_cache = fetched_cache.get(&tx_hash).cloned();
                let snapshot = tx_pool.cloned_snapshot();
                let tip_header = snapshot.tip_header();
                let tx_env = Arc::new(status.with_env(tip_header));
                if let Ok(verified) = verify_rtx(
                    snapshot,
                    Arc::clone(&rtx),
                    tx_env,
                    &verify_cache,
                    max_cycles,
                    None,
                )
                .await
                {
                    let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
                    if let Err(e) = _submit_entry(tx_pool, status, entry, &self.callbacks) {
                        error!("readd_detached_tx submit_entry {} error {}", tx_hash, e);
                    } else {
                        debug!("readd_detached_tx submit_entry {}", tx_hash);
                    }
                }
            }
        }
```

**File:** sync/src/types/mod.rs (L1330-1336)
```rust
    pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
    pending_get_headers: RwLock<LruCache<(PeerIndex, Byte32), Instant>>,
    pending_compact_blocks: tokio::sync::Mutex<PendingCompactBlockMap>,

    /* In-flight items for which we request to peers, but not got the responses yet */
    inflight_proposals: DashMap<packed::ProposalShortId, BlockNumber>,
    inflight_blocks: RwLock<InflightBlocks>,
```
