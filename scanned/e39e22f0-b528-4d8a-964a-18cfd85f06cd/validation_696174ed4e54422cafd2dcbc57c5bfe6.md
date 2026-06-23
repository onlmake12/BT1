### Title
RBF Rejection of a Proposed Transaction Does Not Evict It from the BlockAssembler Template — (`tx-pool/src/process.rs`)

---

### Summary

When a transaction (`tx1`) in `Proposed` status is replaced via RBF, `process_rbf` removes it from the `TxPool` and marks it `Rejected`. However, it does not send a `BlockAssemblerMessage::Proposed` to trigger `update_transactions`. The `BlockAssembler`'s cached `current.template.transactions` still holds `tx1`. The miner's next `get_block_template` call returns a template containing the "rejected" `tx1`, which is then committed on-chain — silently defeating the user's RBF replacement.

This is the direct CKB analog of the LiFi `setContractOwner(address(0))` bug: a "cancellation" operation clears one state layer (the pool entry) but leaves a second pending state layer (the block template cache) intact, allowing the supposedly-cancelled action to complete.

---

### Finding Description

**Root cause — `process_rbf` does not flush the BlockAssembler template**

`_process_tx` in `tx-pool/src/process.rs` is the top-level handler for every submitted transaction. After `submit_entry` succeeds it calls `notify_block_assembler(status)` where `status` is the **new tx's** status: [1](#0-0) 

`notify_block_assembler` maps statuses to messages: [2](#0-1) 

When the replacement tx2 enters the pool as `TxStatus::Fresh` (Pending), the only message sent is `BlockAssemblerMessage::Pending`, which triggers `update_proposals` — **not** `update_transactions`.

Inside `submit_entry`, `process_rbf` removes the conflicting `tx1` from the pool and records it as rejected: [3](#0-2) 

`process_rbf` calls `tx_pool.pool_map.remove_entry_and_descendants`, `tx_pool.record_conflict`, and `callbacks.call_reject` — but it never sends any `BlockAssemblerMessage` to the block assembler. The `BlockAssembler`'s `current.template.transactions` is a cached `Vec<TxEntry>` that is only refreshed when `update_transactions` is called: [4](#0-3) 

`update_transactions` is only invoked on `BlockAssemblerMessage::Proposed`: [5](#0-4) 

Because no `Proposed` message is sent after RBF eviction of a `Proposed` tx, the stale `tx1` entry remains in `current.template.transactions`. The next `get_block_template` call returns it to the miner: [6](#0-5) 

**The test explicitly documents this behaviour:** [7](#0-6) 

The comment at line 718–719 reads:
> *"since old tx is already in BlockAssembler, tx1 will be committed, even it is not in tx_pool and with `Rejected` status now"*

The test asserts `tx1` reaches `Status::Committed` and `tx2` is subsequently `Status::Rejected`.

**Exploit path (step by step)**

1. User submits `tx1` (e.g., payment to address A) via `send_transaction` RPC.
2. A miner proposes `tx1`; it transitions to `Status::Proposed` in the pool. The `BlockAssembler` template is updated with `tx1` via `BlockAssemblerMessage::Proposed` → `update_transactions`.
3. User submits `tx2` (same inputs, higher fee, payment to address B) via `send_transaction` RPC — a valid RBF replacement.
4. `process_rbf` removes `tx1` from the pool; `tx1` receives `Status::Rejected` / `RBFRejected`. The user observes this and believes the replacement succeeded.
5. `notify_block_assembler(TxStatus::Fresh)` sends only `BlockAssemblerMessage::Pending` → `update_proposals`. The template's `transactions` list is **not** refreshed.
6. The miner calls `get_block_template`; the template still contains `tx1`.
7. The miner mines a block committing `tx1`. Funds go to address A.
8. `tx2` is subsequently rejected because its inputs are now spent.

---

### Impact Explanation

A user who successfully submits an RBF replacement receives a `Rejected` status on the original transaction, creating a false guarantee that the replacement has taken effect. The original transaction is nonetheless committed on-chain, spending the user's cells to the original destination. The replacement transaction is then permanently rejected. This constitutes an **unauthorized state change from the user's perspective**: the user's explicit intent (RBF cancellation) is silently overridden by a stale miner template. Financial loss is concrete and irreversible once the block is confirmed.

Severity: **Medium** — requires the original transaction to have already reached `Proposed` status (on-chain proposal), but no privileged access or malicious miner is needed; the stale template is used automatically.

---

### Likelihood Explanation

The window is bounded by the proposal window (`tx_proposal_window.closest()` blocks, default 2). Any user who submits an RBF replacement of a `Proposed` transaction within that window is affected. The behaviour is deterministic and reproducible (confirmed by the existing integration test `RbfRejectReplaceProposed`). No special attacker capability is required — only a normal `send_transaction` RPC call.

---

### Recommendation

After `process_rbf` removes one or more `Proposed` transactions, send `BlockAssemblerMessage::Proposed` to force `update_transactions` to rebuild the template from the current pool state. Concretely, in `submit_entry` (or in `process_rbf` itself), detect whether any removed conflict had `Status::Proposed` and, if so, enqueue a `Proposed` message to the block assembler channel before returning. This mirrors the existing pattern used when a new `Proposed` tx is added.

Alternatively, `update_transactions` could be called unconditionally after any RBF eviction, regardless of the replacement tx's status.

---

### Proof of Concept

The existing integration test `RbfRejectReplaceProposed` in `test/src/specs/tx_pool/replace.rs` is a complete, deterministic PoC: [8](#0-7) 

Key assertions (lines 710, 724, 730):
- `tx1.status == Rejected` immediately after RBF
- `tx1.status == Committed` after `window_count` blocks
- `tx2.status == Rejected` as a consequence

The companion test `RbfReplaceProposedSuccess` (lines 752–877) confirms the workaround: submitting a blank block forces a `BlockAssemblerMessage::Reset` → `update_blank`, which clears the stale template and allows `tx2` to be committed instead. [9](#0-8)

### Citations

**File:** tx-pool/src/process.rs (L66-74)
```rust
    pub(crate) async fn get_block_template(&self) -> Result<BlockTemplate, AnyError> {
        if let Some(ref block_assembler) = self.block_assembler {
            Ok(block_assembler.get_current().await)
        } else {
            Err(InternalErrorKind::Config
                .other("BlockAssembler disabled")
                .into())
        }
    }
```

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

**File:** tx-pool/src/process.rs (L190-234)
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
```

**File:** tx-pool/src/process.rs (L753-756)
```rust
        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);

        self.notify_block_assembler(status).await;
```

**File:** tx-pool/src/block_assembler/mod.rs (L415-447)
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
```

**File:** tx-pool/src/block_assembler/process.rs (L11-16)
```rust
        BlockAssemblerMessage::Proposed => {
            if let Some(ref block_assembler) = service.block_assembler
                && let Err(e) = block_assembler.update_transactions(&service.tx_pool).await
            {
                ckb_logger::error!("block_assembler update_transactions error {}", e);
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

**File:** test/src/specs/tx_pool/replace.rs (L826-865)
```rust

        // submit a blank block
        let example = node0.new_block(None, None, None);
        let blank_block = example
            .as_advanced_builder()
            .set_proposals(vec![])
            .set_transactions(vec![example.transaction(0).unwrap()])
            .build();
        node0.submit_block(&blank_block);

        wait_until(10, move || node0.get_tip_block() == blank_block);

        let window_count = node0.consensus().tx_proposal_window().closest();
        node0.mine(window_count);

        let ret = wait_until(20, || {
            let res = rpc_client0.get_transaction(tx2.hash());
            res.tx_status.status == Status::Proposed
        });
        assert!(ret, "tx2 should be proposed");
        let tx1_status = node0.rpc_client().get_transaction(txs[2].hash()).tx_status;
        assert_eq!(tx1_status.status, Status::Rejected);

        let mut expected = [
            txs[2].hash().into(),
            txs[3].hash().into(),
            txs[4].hash().into(),
        ];
        expected.sort_unstable();
        assert_eq!(get_tx_pool_conflicts(node0), expected);

        let window_count = node0.consensus().tx_proposal_window().closest();
        node0.mine(window_count);
        // since old tx is already in BlockAssembler,
        // tx1 will be committed, even it is not in tx_pool and with `Rejected` status now
        let ret = wait_until(20, || {
            let res = rpc_client0.get_transaction(tx2.hash());
            res.tx_status.status == Status::Committed
        });
        assert!(ret, "tx2 should be committed");
```
