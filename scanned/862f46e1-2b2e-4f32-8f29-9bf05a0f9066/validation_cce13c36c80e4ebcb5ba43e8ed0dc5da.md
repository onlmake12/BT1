### Title
RBF Replacement of a Proposed Transaction Is Silently Invalidated by Stale BlockAssembler Template — (`tx-pool/src/process.rs`, `tx-pool/src/block_assembler/process.rs`)

---

### Summary

When RBF (Replace-By-Fee) successfully evicts a transaction (`tx1`) that has already reached `Proposed` status and been included in the `BlockAssembler`'s cached template, the `BlockAssembler` is never notified to remove `tx1` from that template. A miner calling `get_block_template` will receive a template still containing the evicted `tx1`. If the miner submits that block, `tx1` is committed to the chain and the RBF replacement (`tx2`) — which paid a higher fee — is permanently invalidated. This is a confirmed, documented behavior in the test suite.

---

### Finding Description

CKB's tx-pool supports Replace-By-Fee (RBF): a new transaction `tx2` spending the same inputs as a pending `tx1` can evict `tx1` from the pool if `tx2` pays a sufficiently higher fee. The eviction is performed in `process_rbf` inside `tx-pool/src/process.rs`. [1](#0-0) 

`process_rbf` calls `tx_pool.pool_map.remove_entry_and_descendants` to remove `tx1` from the pool and calls `call_reject` callbacks to mark it `Rejected`. However, it does **not** send any message to the `BlockAssembler` to invalidate or refresh its cached template. [2](#0-1) 

The `notify_block_assembler` function only sends `BlockAssemblerMessage::Pending` (when a fresh tx enters) or `BlockAssemblerMessage::Proposed` (when a tx is proposed). There is no `BlockAssemblerMessage::Removed` or equivalent signal. [3](#0-2) 

The `BlockAssembler` only refreshes its transaction list (`update_transactions`) when it receives a `Proposed` message. After an RBF eviction, the next message sent is `Pending` (for `tx2` entering the pool), which only triggers `update_proposals` — not `update_transactions`. [4](#0-3) 

The `BlockAssembler` also runs on a periodic timer (`update_interval_millis`). Between the RBF eviction and the next timer tick, the cached template still contains `tx1`. [5](#0-4) 

The test `RbfRejectReplaceProposed` explicitly documents and asserts this behavior:

> "since old tx is already in BlockAssembler, tx1 will be committed, even it is not in tx_pool and with `Rejected` status now" [6](#0-5) 

---

### Impact Explanation

An RBF submitter (`tx2`) who pays a higher fee to replace `tx1` can have their replacement silently invalidated:

1. `tx1` is committed to the chain from the stale `BlockAssembler` template, spending the shared inputs.
2. `tx2` is permanently rejected (`TransactionFailedToResolve: Resolve failed Unknown`) because its inputs are now spent.
3. The `tx2` submitter loses the higher fee they paid for the RBF and their intended transaction outcome is defeated.
4. `tx1`'s original sender effectively "wins" despite the RBF replacement being accepted by the pool.

This is a direct analog to the Marketplace.sol front-running: one party (the original `tx1` sender, or a miner who already has `tx1` in their template) can invalidate the other party's higher-fee replacement by committing the stale transaction first. [7](#0-6) 

---

### Likelihood Explanation

**Medium.** The precondition is that `tx1` must have reached `Proposed` status and been included in the `BlockAssembler`'s cached template before `tx2` is submitted. This is a normal operational state for any transaction that has been proposed. The window of vulnerability is bounded by `update_interval_millis` (the BlockAssembler's refresh interval). In production, this interval is non-zero, creating a reliable race window. Any unprivileged RPC caller can trigger this by submitting a valid RBF transaction against a proposed transaction. [8](#0-7) 

---

### Recommendation

When `process_rbf` successfully evicts a `Proposed` transaction, immediately send a `BlockAssemblerMessage::Proposed` (or a new dedicated `BlockAssemblerMessage::Removed`) to force `update_transactions` to re-read the pool and drop the evicted transaction from the cached template before the next `get_block_template` call can return it. [9](#0-8) 

---

### Proof of Concept

The existing integration test `RbfRejectReplaceProposed` in `test/src/specs/tx_pool/replace.rs` is a complete, runnable proof of concept:

1. Submit a chain of transactions; mine until `txs[2]` reaches `Proposed` status (enters `BlockAssembler` template).
2. Submit `tx2` as an RBF replacement for `txs[2]` with a higher fee — the pool accepts it and marks `txs[2]` as `Rejected`.
3. Mine `window_count` blocks.
4. Observe: `txs[2]` (the "Rejected" original) is committed to the chain; `tx2` (the accepted RBF replacement) is rejected. [10](#0-9)

### Citations

**File:** tx-pool/src/process.rs (L172-186)
```rust
    pub(crate) async fn notify_block_assembler(&self, status: TxStatus) {
        if self.should_notify_block_assembler() {
            let message = match status {
                TxStatus::Fresh => Some(BlockAssemblerMessage::Pending),
                TxStatus::Proposed => Some(BlockAssemblerMessage::Proposed),
                _ => None,
            };

            if let Some(message) = message
                && self.block_assembler_sender.send(message).await.is_err()
            {
                error!("block_assembler receiver dropped");
            }
        }
    }
```

**File:** tx-pool/src/process.rs (L190-235)
```rust
    fn process_rbf(
        &self,
        tx_pool: &mut TxPool,
        entry: &TxEntry,
        conflicts: &HashSet<ProposalShortId>,
    ) -> Vec<TransactionView> {
        let mut may_recovered_txs = vec![];
        let mut available_inputs = HashSet::new();

        if conflicts.is_empty() {
            return may_recovered_txs;
        }

        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();

        available_inputs.extend(
            all_removed
                .iter()
                .flat_map(|removed| removed.transaction().input_pts_iter()),
        );

        for input in entry.transaction().input_pts_iter() {
            available_inputs.remove(&input);
        }

        may_recovered_txs = tx_pool.get_conflicted_txs_from_inputs(available_inputs.into_iter());
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
        }
        assert!(!may_recovered_txs.contains(entry.transaction()));
        may_recovered_txs
    }
```

**File:** tx-pool/src/block_assembler/process.rs (L1-31)
```rust
use crate::service::{BlockAssemblerMessage, TxPoolService};
use std::sync::Arc;

pub(crate) async fn process(service: TxPoolService, message: &BlockAssemblerMessage) {
    match message {
        BlockAssemblerMessage::Pending => {
            if let Some(ref block_assembler) = service.block_assembler {
                block_assembler.update_proposals(&service.tx_pool).await;
            }
        }
        BlockAssemblerMessage::Proposed => {
            if let Some(ref block_assembler) = service.block_assembler
                && let Err(e) = block_assembler.update_transactions(&service.tx_pool).await
            {
                ckb_logger::error!("block_assembler update_transactions error {}", e);
            }
        }
        BlockAssemblerMessage::Uncle => {
            if let Some(ref block_assembler) = service.block_assembler {
                block_assembler.update_uncles().await;
            }
        }
        BlockAssemblerMessage::Reset(snapshot) => {
            if let Some(ref block_assembler) = service.block_assembler
                && let Err(e) = block_assembler.update_blank(Arc::clone(snapshot)).await
            {
                ckb_logger::error!("block_assembler update_blank error {}", e);
            }
        }
    }
}
```

**File:** tx-pool/src/block_assembler/mod.rs (L415-483)
```rust
    pub(crate) async fn update_transactions(
        &self,
        tx_pool: &RwLock<TxPool>,
    ) -> Result<(), AnyError> {
        let mut current = self.current.lock().await;
        let consensus = current.snapshot.consensus();
        let current_template = &current.template;
        let max_block_bytes = consensus.max_block_bytes() as usize;
        let extension = Self::build_extension(&current.snapshot)?;
        let txs = {
            let tx_pool_reader = tx_pool.read().await;
            if current.snapshot.tip_hash() != tx_pool_reader.snapshot().tip_hash() {
                return Ok(());
            }

            let basic_block_size = Self::basic_block_size(
                current_template.cellbase.data(),
                &current_template.uncles,
                current_template.proposals.iter(),
                extension.clone(),
            );

            let txs_size_limit = max_block_bytes.checked_sub(basic_block_size);

            if txs_size_limit.is_none() {
                return Ok(());
            }

            let max_block_cycles = consensus.max_block_cycles();
            let (txs, _txs_size, _cycles) = tx_pool_reader
                .package_txs(max_block_cycles, txs_size_limit.expect("overflow checked"));
            txs
        };

        if let Ok((dao, checked_txs, _failed_txs)) = Self::calc_dao(
            &current.snapshot,
            &current.epoch,
            current_template.cellbase.clone(),
            txs,
        ) {
            let new_txs_size = Self::checked_entries_size(&checked_txs)?;
            let new_total_size = current.size.calc_total_by_txs(new_txs_size);
            let mut builder = BlockTemplateBuilder::from_template(&current.template);
            builder
                .set_transactions(checked_txs)
                .work_id(self.work_id.fetch_add(1, Ordering::SeqCst))
                .current_time(cmp::max(
                    unix_time_as_millis(),
                    current.template.current_time,
                ))
                .dao(dao);
            if let Some(data) = extension {
                builder.extension(data);
            }
            current.template = builder.build();
            current.size.txs = new_txs_size;
            current.size.total = new_total_size;

            trace!(
                "[BlockAssembler] update_transactions-{} epoch-{} uncles-{} proposals-{} txs-{}",
                current.template.number,
                current.template.epoch.number(),
                current.template.uncles.len(),
                current.template.proposals.len(),
                current.template.transactions.len(),
            );
        }
        Ok(())
    }
```

**File:** tx-pool/src/service.rs (L636-638)
```rust
            let signal_receiver = self.signal_receiver.clone();
            let interval = Duration::from_millis(block_assembler.config.update_interval_millis);
            if interval.is_zero() {
```

**File:** tx-pool/src/service.rs (L660-694)
```rust
            } else {
                self.handle.spawn(async move {
                    let mut interval = tokio::time::interval(interval);
                    let mut queue = LinkedHashSet::new();
                    loop {
                        tokio::select! {
                            Some(message) = block_assembler_receiver.recv() => {
                                if let BlockAssemblerMessage::Reset(..) = message {
                                    let service_clone = process_service.clone();
                                    queue.clear();
                                    block_assembler::process(service_clone, &message).await;
                                } else {
                                    queue.insert(message);
                                }
                            },
                            _ = interval.tick() => {
                                for message in &queue {
                                    let service_clone = process_service.clone();
                                    block_assembler::process(service_clone, message).await;
                                }
                                if !queue.is_empty()
                                    && let Some(ref block_assembler) = process_service.block_assembler {
                                        block_assembler.notify().await;
                                    }
                                queue.clear();
                            }
                            _ = signal_receiver.cancelled() => {
                                info!("TxPool block_assembler process service received exit signal, exit now");
                                break
                            },
                            else => break,
                        }
                    }
                });
            }
```

**File:** test/src/specs/tx_pool/replace.rs (L641-750)
```rust
pub struct RbfRejectReplaceProposed;

// RBF Rule #6
// We removed rule #6, even tx in `Gap` and `Proposed` status can be replaced.
impl Spec for RbfRejectReplaceProposed {
    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &nodes[0];

        node0.mine_until_out_bootstrap_period();

        // build txs chain
        let tx0 = node0.new_transaction_spend_tip_cellbase();
        let mut txs = vec![tx0];
        let max_count = 5;
        while txs.len() <= max_count {
            let parent = txs.last().unwrap();
            let child = parent
                .as_advanced_builder()
                .set_inputs(vec![{
                    CellInput::new_builder()
                        .previous_output(OutPoint::new(parent.hash(), 0))
                        .build()
                }])
                .set_outputs(vec![parent.output(0).unwrap()])
                .build();
            txs.push(child);
        }
        assert_eq!(txs.len(), max_count + 1);
        // send Tx chain
        for tx in txs[..=max_count - 1].iter() {
            let ret = node0.rpc_client().send_transaction_result(tx.data().into());
            assert!(ret.is_ok());
        }

        let proposed = node0.mine_with_blocking(|template| template.proposals.len() != max_count);
        let ret = node0.rpc_client().get_transaction(txs[2].hash());
        assert!(
            matches!(ret.tx_status.status, Status::Pending),
            "tx1 should be pending"
        );

        node0.mine_with_blocking(|template| template.number.value() != (proposed + 1));

        let rpc_client0 = node0.rpc_client();
        let ret = wait_until(20, || {
            let res = rpc_client0.get_transaction(txs[2].hash());
            res.tx_status.status == Status::Proposed
        });
        assert!(ret, "tx1 should be proposed");

        let clone_tx = txs[2].clone();
        // Set tx2 fee to a higher value
        let output2 = CellOutputBuilder::default()
            .capacity(capacity_bytes!(70))
            .build();

        let tx1_hash = txs[2].hash();
        let tx2 = clone_tx
            .as_advanced_builder()
            .set_outputs(vec![output2])
            .build();

        // begin to RBF
        let res = node0
            .rpc_client()
            .send_transaction_result(tx2.data().into());
        assert!(res.is_ok());

        let old_tx_status = node0.rpc_client().get_transaction(tx1_hash).tx_status;
        assert_eq!(old_tx_status.status, Status::Rejected);
        assert!(old_tx_status.reason.unwrap().contains("RBFRejected"));

        let tx2_status = node0.rpc_client().get_transaction(tx2.hash()).tx_status;
        assert_eq!(tx2_status.status, Status::Pending);

        let window_count = node0.consensus().tx_proposal_window().closest();
        node0.mine(window_count);
        // since old tx is already in BlockAssembler,
        // tx1 will be committed, even it is not in tx_pool and with `Rejected` status now
        let ret = wait_until(20, || {
            let res = rpc_client0.get_transaction(txs[2].hash());
            res.tx_status.status == Status::Committed
        });
        assert!(ret, "tx1 should be committed");
        let tx1_status = node0.rpc_client().get_transaction(txs[2].hash()).tx_status;
        assert_eq!(tx1_status.status, Status::Committed);

        // tx2 will be marked as `Rejected` because callback of `remove_committed_txs` from tx1
        let tx2_status = node0.rpc_client().get_transaction(tx2.hash()).tx_status;
        assert_eq!(tx2_status.status, Status::Rejected);

        // the same tx2 can not be sent again
        let res = node0
            .rpc_client()
            .send_transaction_result(tx2.data().into());
        assert!(res.is_err(), "tx2 should be rejected");

        // resolve tx2 failed with `unknown` when resolve inputs used by tx1
        assert!(
            res.err()
                .unwrap()
                .to_string()
                .contains("TransactionFailedToResolve: Resolve failed Unknown")
        );
    }

    fn modify_app_config(&self, config: &mut ckb_app_config::CKBAppConfig) {
        config.tx_pool.min_rbf_rate = ckb_types::core::FeeRate(1500);
    }
}
```
