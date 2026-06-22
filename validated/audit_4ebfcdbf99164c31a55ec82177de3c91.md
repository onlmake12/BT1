### Title
Proposed Transaction Committed After `remove_transaction` Due to Stale `BlockAssembler` Cache — (`tx-pool/src/block_assembler/mod.rs`)

---

### Summary

The `remove_transaction` RPC removes a transaction from the tx pool but does not synchronously invalidate the `BlockAssembler`'s cached `CurrentTemplate`. If a proposed transaction is already embedded in the cached block template, a miner can still commit it to the chain after the operator has called `remove_transaction`, making the cancellation ineffective.

---

### Finding Description

CKB's two-phase commit protocol requires a transaction to be **proposed** in one block and **committed** in a later block within the proposal window. The `BlockAssembler` maintains a cached `CurrentTemplate` struct: [1](#0-0) 

This cache is updated **asynchronously** only when specific `BlockAssemblerMessage` variants are received (`Pending`, `Proposed`, `Reset`): [2](#0-1) 

When `remove_transaction` is called via RPC, the implementation in `TxPoolService::remove_tx` removes the transaction from the verify queue, orphan pool, and tx pool: [3](#0-2) 

**Critically, no `BlockAssemblerMessage` is sent to invalidate the cached template.** The `BlockAssembler`'s `current` mutex still holds the old `CurrentTemplate` containing the removed transaction. When the miner subsequently calls `get_block_template`, it receives the stale template via: [4](#0-3) 

The miner mines and submits a block containing the "removed" transaction. The `TwoPhaseCommitVerifier` accepts it because the transaction was validly proposed on-chain: [5](#0-4) 

This behavior is explicitly confirmed in the integration test suite with the comment: [6](#0-5) 

> *"since old tx is already in BlockAssembler, tx1 will be committed, even it is not in tx_pool and with `Rejected` status now"*

The same pattern is confirmed for the `RbfReplaceProposedSuccess` test case: [7](#0-6) 

---

### Impact Explanation

A node operator (or automated system) that calls `remove_transaction` to prevent a proposed transaction from being committed — for example, after discovering a policy violation, a double-spend attempt, or an RBF conflict — **cannot guarantee the transaction will not be committed**. The `BlockAssembler`'s cached template acts as a window during which the miner can still produce and submit a valid block containing the removed transaction. The committed block passes all consensus verification because the transaction was legitimately proposed on-chain.

The concrete consequence: the operator's "cancel" action (`remove_transaction`) is silently ineffective if the block assembler has already snapshotted the transaction into its cached template. The transaction is committed to the canonical chain, and any conflicting transaction the operator intended to replace it with is then rejected.

---

### Likelihood Explanation

The window of vulnerability exists between:
1. The moment the `BlockAssembler` last ran `update_transactions` or `update_full` (capturing the proposed tx into the cached template), and
2. The next time the template is refreshed (either by a new block arriving, triggering `BlockAssemblerMessage::Reset`, or by the periodic `update_interval_millis` tick).

In a solo-mining or pool-mining setup where the node is also the miner, this window is always present for any proposed transaction. The `update_interval_millis` configuration controls how long the stale template persists. The behavior is reproducible and confirmed by existing integration tests.

---

### Recommendation

When `remove_transaction` is called, the implementation should send a `BlockAssemblerMessage::Reset` (or a new dedicated invalidation message) to the block assembler to force a template rebuild before the next `get_block_template` call returns. This mirrors how `clear_pool` already sends a `Reset` message: [8](#0-7) 

Alternatively, `get_block_template` could re-validate each transaction in the cached template against the current pool state before returning it to the miner.

---

### Proof of Concept

The existing integration test `RbfReplaceProposedSuccess` (and the related `RbfReplaceProposedTx` test) directly demonstrate the issue:

1. Transaction `tx1` is submitted, proposed, and enters `Proposed` status in the pool.
2. The `BlockAssembler` captures `tx1` into its cached template via `update_transactions`.
3. An RBF replacement (`tx2`) is submitted, which evicts `tx1` from the pool with `Rejected` status.
4. Despite `tx1` being `Rejected` in the pool, `node0.mine(window_count)` produces a block committing `tx1` — because the miner's `get_block_template` returns the stale cached template.
5. `tx1` is confirmed as `Committed` on-chain; `tx2` is then rejected as a conflict. [9](#0-8) 

The same sequence applies when `remove_transaction` is called directly instead of via RBF: the pool removal does not flush the block assembler cache, leaving the transaction committable by the miner.

### Citations

**File:** tx-pool/src/block_assembler/mod.rs (L93-109)
```rust
#[derive(Clone)]
pub(crate) struct CurrentTemplate {
    pub(crate) template: BlockTemplate,
    pub(crate) size: TemplateSize,
    pub(crate) snapshot: Arc<Snapshot>,
    pub(crate) epoch: EpochExt,
}

/// Block generator
#[derive(Clone)]
pub struct BlockAssembler {
    pub(crate) config: Arc<BlockAssemblerConfig>,
    pub(crate) work_id: Arc<AtomicU64>,
    pub(crate) candidate_uncles: Arc<Mutex<CandidateUncles>>,
    pub(crate) current: Arc<Mutex<CurrentTemplate>>,
    pub(crate) poster: Arc<Client<HttpConnector, Full<bytes::Bytes>>>,
}
```

**File:** tx-pool/src/block_assembler/mod.rs (L485-488)
```rust
    pub(crate) async fn get_current(&self) -> JsonBlockTemplate {
        let current = self.current.lock().await;
        (&current.template).into()
    }
```

**File:** tx-pool/src/block_assembler/process.rs (L4-31)
```rust
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

**File:** tx-pool/src/process.rs (L916-930)
```rust
    pub(crate) async fn clear_pool(&mut self, new_snapshot: Arc<Snapshot>) {
        {
            let mut tx_pool = self.tx_pool.write().await;
            tx_pool.clear(Arc::clone(&new_snapshot));
        }
        // reset block_assembler
        if self
            .block_assembler_sender
            .send(BlockAssemblerMessage::Reset(new_snapshot))
            .await
            .is_err()
        {
            error!("block_assembler receiver dropped");
        }
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L146-214)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        if self.block.is_genesis() {
            return Ok(());
        }
        let block_number = self.block.header().number();
        let proposal_window = self.context.consensus.tx_proposal_window();
        let proposal_start = block_number.saturating_sub(proposal_window.farthest());
        let mut proposal_end = block_number.saturating_sub(proposal_window.closest());

        let mut block_hash = self
            .context
            .store
            .get_block_hash(proposal_end)
            .ok_or(CommitError::AncestorNotFound)?;

        let mut proposal_txs_ids = HashSet::new();

        while proposal_end >= proposal_start {
            let header = self
                .context
                .store
                .get_block_header(&block_hash)
                .ok_or(CommitError::AncestorNotFound)?;
            if header.is_genesis() {
                break;
            }

            if let Some(ids) = self.context.store.get_block_proposal_txs_ids(&block_hash) {
                proposal_txs_ids.extend(ids);
            }
            if let Some(uncles) = self.context.store.get_block_uncles(&block_hash) {
                uncles
                    .data()
                    .into_iter()
                    .for_each(|uncle| proposal_txs_ids.extend(uncle.proposals()));
            }

            block_hash = header.data().raw().parent_hash();
            proposal_end -= 1;
        }

        let committed_ids: HashSet<_> = self
            .block
            .transactions()
            .iter()
            .skip(1)
            .map(TransactionView::proposal_short_id)
            .collect();

        if committed_ids.difference(&proposal_txs_ids).next().is_some() {
            error_target!(
                crate::LOG_TARGET,
                "BlockView {} {}",
                self.block.number(),
                self.block.hash()
            );
            error_target!(crate::LOG_TARGET, "proposal_window {:?}", proposal_window);
            error_target!(crate::LOG_TARGET, "Committed Ids:");
            for committed_id in committed_ids.iter() {
                error_target!(crate::LOG_TARGET, "    {:?}", committed_id);
            }
            error_target!(crate::LOG_TARGET, "Proposal Txs Ids:");
            for proposal_txs_id in proposal_txs_ids.iter() {
                error_target!(crate::LOG_TARGET, "    {:?}", proposal_txs_id);
            }
            return Err((CommitError::Invalid).into());
        }
        Ok(())
    }
```

**File:** test/src/specs/tx_pool/replace.rs (L716-730)
```rust
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
```

**File:** test/src/specs/tx_pool/replace.rs (L857-865)
```rust
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
