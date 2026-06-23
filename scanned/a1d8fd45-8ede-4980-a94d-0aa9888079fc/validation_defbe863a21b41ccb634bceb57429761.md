### Title
Missing IBD State Guard in `send_transaction` RPC Allows Transaction Submission Against Incomplete Chain State - (File: `rpc/src/module/pool.rs`)

### Summary

The `send_transaction` RPC endpoint accepts and processes transactions into the tx-pool without checking whether the node is in Initial Block Download (IBD) mode. During IBD the node's UTXO snapshot is incomplete, so the pool validates transactions against a stale view of live cells. Transactions accepted during IBD may be silently evicted once IBD completes and the pool is reconciled against the fully-synced chain, giving callers a false positive acceptance signal.

### Finding Description

CKB's IBD state is the direct structural analog to Alchemix's "emergency exit" state: it is a well-defined critical operational mode in which the node's chain view is known to be incomplete and unreliable. The codebase already enforces IBD guards in several places:

- The relay protocol (`sync/src/relayer/mod.rs`) returns immediately on both `received` and `notify` if IBD is active, refusing to process any relay message including transaction relay.
- The synchronizer (`sync/src/synchronizer/mod.rs`) gates block-fetch tokens on the IBD flag.
- The chain verifier (`chain/src/verify.rs`) reads `is_initial_block_download()` and propagates the state to the tx-pool via `update_ibd_state`.

However, the `send_transaction` RPC handler in `rpc/src/module/pool.rs` performs no such check:

```rust
fn send_transaction(
    &self,
    tx: Transaction,
    outputs_validator: Option<OutputsValidator>,
) -> Result<H256> {
    let tx: packed::Transaction = tx.into();
    let tx: core::TransactionView = tx.into_view();

    self.check_output_validator(outputs_validator, &tx)?;

    let tx_pool = self.shared.tx_pool_controller();
    let submit_tx = tx_pool.submit_local_tx(tx.clone());
    // ... returns Ok(tx_hash) on acceptance
```

`self.shared` exposes `is_initial_block_download()` (defined in `shared/src/shared.rs`), but it is never consulted here. The transaction is forwarded directly to `submit_local_tx`, which validates it against the current snapshot — a snapshot that is incomplete during IBD.

The same omission exists in `get_block_template` (`rpc/src/module/miner.rs`), which also calls through to `self.shared.get_block_template(...)` without an IBD guard, allowing miners to assemble blocks on a stale tip.

### Impact Explanation

An RPC caller (local CLI user, wallet, or DApp) submitting a transaction during IBD receives `Ok(tx_hash)` — a positive acceptance signal — even though:

1. The snapshot used for cell resolution is incomplete; cells that appear live locally may already be spent in blocks the node has not yet downloaded.
2. As IBD progresses, `update_tx_pool_for_reorg` is called for each new best block, which may evict the accepted transaction without any notification to the caller.
3. A caller that acts on the acceptance signal (e.g., considers a payment sent, releases goods, or chains a dependent transaction) may suffer financial loss when the transaction is silently dropped.

This mirrors the Alchemix pattern exactly: a state-changing operation (pool admission) is permitted during a critical state (IBD) where the preconditions for that operation cannot be reliably verified.

### Likelihood Explanation

Any unprivileged RPC caller can trigger this. IBD occurs on every fresh node start and after any restart where the tip timestamp has fallen behind `MAX_TIP_AGE`. Wallets and DApps that do not separately poll `get_blockchain_info` for `is_initial_block_download` before calling `send_transaction` will silently hit this path. The condition is not rare or contrived.

### Recommendation

Add an IBD guard at the top of `send_transaction` (and `get_block_template`) using the already-available `is_initial_block_download()` method:

```rust
fn send_transaction(&self, tx: Transaction, outputs_validator: Option<OutputsValidator>) -> Result<H256> {
    if self.shared.is_initial_block_download() {
        return Err(RPCError::custom(
            RPCError::Invalid,
            "Node is in initial block download; transaction submission is not available".into(),
        ));
    }
    // ... existing logic
}
```

Alternatively, expose the IBD check as a shared modifier/helper so it can be applied consistently across all state-sensitive RPC handlers, mirroring the pattern already used in the relay and sync protocols.

### Proof of Concept

1. Start a fresh CKB node (IBD is active; `get_blockchain_info` returns `"is_initial_block_download": true`).
2. Call `send_transaction` with any structurally valid transaction whose inputs appear live in the genesis/early snapshot.
3. Observe that the call returns `Ok(tx_hash)` — no IBD rejection.
4. Call `get_transaction` on the returned hash; the transaction is present in the pool with status `pending`.
5. Wait for IBD to complete; call `get_transaction` again — the transaction has been silently evicted if its inputs were spent in any of the downloaded blocks.

**Root cause line references:**

- Missing guard: [1](#0-0) 
- Available guard method: [2](#0-1) 
- Correct IBD guard pattern (relay): [3](#0-2) 
- Correct IBD guard pattern (notify): [4](#0-3) 
- Missing guard in miner RPC: [5](#0-4) 
- IBD state propagation to tx-pool (shows intent to track IBD): [6](#0-5)

### Citations

**File:** rpc/src/module/pool.rs (L612-635)
```rust
    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();
        let submit_tx = tx_pool.submit_local_tx(tx.clone());

        if let Err(e) = submit_tx {
            error!("Send submit_tx request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        }

        let tx_hash = tx.hash();
        match submit_tx.unwrap() {
            Ok(_) => Ok(tx_hash.into()),
            Err(reject) => Err(RPCError::from_submit_transaction_reject(&reject)),
        }
    }
```

**File:** shared/src/shared.rs (L382-394)
```rust
    pub fn is_initial_block_download(&self) -> bool {
        // Once this function has returned false, it must remain false.
        if self.ibd_finished.load(Ordering::Acquire) {
            false
        } else if unix_time_as_millis().saturating_sub(self.snapshot().tip_header().timestamp())
            > MAX_TIP_AGE
        {
            true
        } else {
            self.ibd_finished.store(true, Ordering::Release);
            false
        }
    }
```

**File:** sync/src/relayer/mod.rs (L815-818)
```rust
        // If self is in the IBD state, don't process any relayer message.
        if self.shared.active_chain().is_initial_block_download() {
            return;
        }
```

**File:** sync/src/relayer/mod.rs (L938-941)
```rust
        // If self is in the IBD state, don't trigger any relayer notify.
        if self.shared.active_chain().is_initial_block_download() {
            return;
        }
```

**File:** rpc/src/module/miner.rs (L238-258)
```rust
    fn get_block_template(
        &self,
        bytes_limit: Option<Uint64>,
        proposals_limit: Option<Uint64>,
        max_version: Option<Version>,
    ) -> Result<BlockTemplate> {
        let bytes_limit = bytes_limit.map(|b| b.into());

        let proposals_limit = proposals_limit.map(|b| b.into());

        self.shared
            .get_block_template(bytes_limit, proposals_limit, max_version.map(Into::into))
            .map_err(|err| {
                error!("Send get_block_template request error {}", err);
                RPCError::ckb_internal_error(err)
            })?
            .map_err(|err| {
                error!("Get_block_template result error {}", err);
                RPCError::from_any_error(err)
            })
    }
```

**File:** chain/src/verify.rs (L330-397)
```rust
        let in_ibd = self.shared.is_initial_block_download();

        if new_best_block {
            info!(
                "[verify block] new best block found: {} => {:#x}, difficulty diff = {:#x}, unverified_tip: {}",
                block.header().number(),
                block.header().hash(),
                &cannon_total_difficulty - &current_total_difficulty,
                self.shared.get_unverified_tip().number(),
            );
            self.find_fork(&mut fork, current_tip_header.number(), block, ext);
            self.rollback(&fork, &db_txn)?;

            // update and verify chain root
            // MUST update index before reconcile_main_chain
            let begin_reconcile_main_chain = std::time::Instant::now();
            self.reconcile_main_chain(Arc::clone(&db_txn), &mut fork, switch)?;
            trace!(
                "reconcile_main_chain cost {:?}",
                begin_reconcile_main_chain.elapsed()
            );

            db_txn.insert_tip_header(&block.header())?;
            if new_epoch || fork.has_detached() {
                db_txn.insert_current_epoch_ext(&epoch)?;
            }
        } else {
            db_txn.insert_block_ext(&block.header().hash(), &ext)?;
        }
        db_txn.commit()?;

        if new_best_block {
            let tip_header = block.header();
            info!(
                "block: {}, hash: {:#x}, epoch: {:#}, total_diff: {:#x}, txs: {}, proposals: {}",
                tip_header.number(),
                tip_header.hash(),
                tip_header.epoch(),
                cannon_total_difficulty,
                block.transactions().len(),
                block.data().proposals().len()
            );

            self.update_proposal_table(&fork);
            let (detached_proposal_id, new_proposals) = self
                .proposal_table
                .finalize(origin_proposals, tip_header.number());
            fork.detached_proposal_id = detached_proposal_id;

            let new_snapshot =
                self.shared
                    .new_snapshot(tip_header, cannon_total_difficulty, epoch, new_proposals);

            self.shared.store_snapshot(Arc::clone(&new_snapshot));

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
```
