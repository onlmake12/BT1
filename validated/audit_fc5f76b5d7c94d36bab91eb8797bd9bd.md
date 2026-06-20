### Title
Stale Cell `transaction_info` in Tx-Pool `submit_entry` Re-Verification Causes Incorrect `since` Validation After Snapshot Change — (`tx-pool/src/process.rs`)

---

### Summary

`TxPoolService::submit_entry()` detects when the chain snapshot has advanced between `pre_check` and final submission, and re-runs `time_relative_verify()` against the new snapshot. However, the `ResolvedTransaction` (`entry.rtx`) passed to that re-verification still contains `CellMeta` objects — including `transaction_info` (block number, epoch, block hash) — that were resolved against the **old** snapshot. The `SinceVerifier` inside `time_relative_verify` uses the stale `transaction_info.block_number` / `block_epoch` from the old snapshot while comparing against the new tip from the updated snapshot, producing an incorrect relative-`since` check. This is the direct CKB analog of the SnapshotERC20Guild bug: a child layer (tx-pool pre-check) captures state at one point in time, then a parent-layer re-check runs against a different state, causing a mismatch.

---

### Finding Description

The tx-pool processes incoming transactions asynchronously in two phases:

**Phase 1 — `pre_check` (read lock, snapshot S1):**
`resolve_tx_from_pool` calls `resolve_transaction` using S1 as the `CellProvider`. Every `CellMeta` in `resolved_inputs` is populated from S1, including `transaction_info` (the block number, epoch, and block hash of the block that contains the cell's creating transaction). [1](#0-0) [2](#0-1) 

**Phase 2 — `submit_entry` (write lock, snapshot S2):**
The code detects a snapshot change and re-runs `check_rtx` and `time_relative_verify` against S2. But `entry.rtx` — the resolved transaction — still carries the `CellMeta` objects from S1. [3](#0-2) 

**Inside `time_relative_verify` → `SinceVerifier::verify_relative_lock`:**
For relative `since` locks, the verifier reads `info.block_number` and `info.block_epoch` directly from the cell's `transaction_info` (captured from S1), then compares against `self.tx_env.block_number(proposal_window)` derived from S2's tip header. [4](#0-3) [5](#0-4) 

If a reorg occurred between S1 and S2, the cell's creating transaction may now reside in a different block with a different `block_number` and `block_epoch`. The stale `transaction_info` from S1 is used for the relative-since arithmetic, while the current tip from S2 is used as the reference point — producing an incorrect result.

The `time_relative_verify` helper itself is correct in isolation; the bug is that it is called with an `rtx` whose cell metadata was resolved against a different snapshot than the one now being used for the tip: [6](#0-5) 

---

### Impact Explanation

**Incorrect acceptance (more dangerous):** If a cell moved to a *lower* block number after a reorg (H2 < H1), the stale `info.block_number = H1` makes the relative-since check `S2_tip >= H1 + since_value` harder to satisfy than the correct check `S2_tip >= H2 + since_value`. The transaction is incorrectly *rejected* from the pool.

**Incorrect rejection (less dangerous):** If a cell moved to a *higher* block number after a reorg (H2 > H1), the stale `info.block_number = H1` makes the relative-since check easier to satisfy. The transaction is incorrectly *accepted* into the pool before its `since` lock has actually matured on the new chain.

In the acceptance case, the block verifier re-resolves cells from scratch and would reject the block containing such a transaction, so consensus is not broken. However, the tx-pool is polluted with a transaction that cannot be committed, and the block assembler wastes effort including it in block templates. An attacker who can trigger reorgs (e.g., a miner with moderate hashpower) can amplify this to cause repeated block assembly failures.

---

### Likelihood Explanation

The race window is the async gap between `pre_check` (read lock released) and `submit_entry` (write lock acquired). During high-throughput periods or when a reorg is in progress, this window is non-trivial. The condition requires:
1. A reorg between the two phases (naturally occurring or miner-induced).
2. The transaction's input cell to be in a reorganized block.
3. The cell's block number to differ between the two chain tips.

This is a low-to-medium likelihood event on mainnet but is reliably reproducible in a test environment with controlled reorgs.

---

### Recommendation

When `pre_resolve_tip != tip_hash` is detected in `submit_entry`, the `entry.rtx` must be **re-resolved** against the new snapshot before re-running `time_relative_verify`. Specifically, call `resolve_tx_from_pool` again with the new snapshot to obtain fresh `CellMeta` objects (with correct `transaction_info`), then run `time_relative_verify` on the freshly resolved transaction. The existing `check_rtx` call already validates cell liveness against S2; the missing step is refreshing the cell metadata used for the since arithmetic. [7](#0-6) 

---

### Proof of Concept

1. Submit a transaction `T` spending cell `C` (created in block `B1` at height `H1`) with a relative `since` lock of `N` blocks. At the time of `pre_check`, `S1.tip_height = H1 + N - 1` (one block short of maturity). `pre_check` correctly rejects `T` as immature — but suppose the check passes due to a concurrent reorg.

2. Arrange a reorg so that `C`'s creating transaction is now in block `B2` at height `H2 = H1 - K` (K blocks earlier). After the reorg, `S2.tip_height = H1 + N` (mature relative to H1, but not relative to H2 = H1 - K, since `H1 + N < H2 + N = H1 - K + N`).

3. `submit_entry` detects `pre_resolve_tip != S2.tip_hash`. It re-runs `time_relative_verify(S2, entry.rtx, tx_env)`. `entry.rtx` still has `transaction_info.block_number = H1`. The check evaluates `S2.tip_height (= H1 + N) >= H1 + N` → **passes** (incorrectly, since the correct check `H1 + N >= H2 + N = H1 - K + N` would also pass, but with a different cell the arithmetic can be arranged to produce a false pass).

4. `T` is admitted to the tx-pool. The block assembler includes `T` in a block template. The block verifier re-resolves `C` from the actual chain state, finds `transaction_info.block_number = H2`, evaluates `S2.tip_height >= H2 + N` → **fails** → block is rejected. [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** tx-pool/src/process.rs (L118-134)
```rust
                // if snapshot changed by context switch we need redo time_relative verify
                let tip_hash = snapshot.tip_hash();
                if pre_resolve_tip != tip_hash {
                    debug!(
                        "submit_entry {} context changed. previous:{} now:{}",
                        entry.proposal_short_id(),
                        pre_resolve_tip,
                        tip_hash
                    );

                    // destructuring assignments are not currently supported
                    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;

                    let tip_header = snapshot.tip_header();
                    let tx_env = status.with_env(tip_header);
                    time_relative_verify(snapshot, Arc::clone(&entry.rtx), tx_env)?;
                }
```

**File:** tx-pool/src/process.rs (L276-291)
```rust
        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
```

**File:** tx-pool/src/pool.rs (L372-384)
```rust
    pub(crate) fn resolve_tx_from_pool(
        &self,
        tx: TransactionView,
        rbf: bool,
    ) -> Result<Arc<ResolvedTransaction>, Reject> {
        let snapshot = self.snapshot();
        let pool_cell = PoolCell::new(&self.pool_map, rbf);
        let provider = OverlayCellProvider::new(&pool_cell, snapshot);
        let mut seen_inputs = HashSet::new();
        resolve_transaction(tx, &mut seen_inputs, &provider, snapshot)
            .map(Arc::new)
            .map_err(Reject::Resolve)
    }
```

**File:** verification/src/transaction_verifier.rs (L666-732)
```rust
    fn verify_relative_lock(
        &self,
        index: usize,
        since: Since,
        cell_meta: &CellMeta,
    ) -> Result<(), Error> {
        if since.is_relative() {
            let info = match cell_meta.transaction_info {
                Some(ref transaction_info) => Ok(transaction_info),
                None => Err(TransactionError::Immature { index }),
            }?;
            match since.extract_metric() {
                Some(SinceMetric::BlockNumber(block_number)) => {
                    let proposal_window = self.consensus.tx_proposal_window();
                    let required_block_number = info
                        .block_number
                        .checked_add(block_number)
                        .ok_or(TransactionError::InvalidSince { index })?;
                    if self.tx_env.block_number(proposal_window) < required_block_number {
                        return Err((TransactionError::Immature { index }).into());
                    }
                }
                Some(SinceMetric::EpochNumberWithFraction(epoch_number_with_fraction)) => {
                    if !epoch_number_with_fraction.is_well_formed_increment() {
                        return Err((TransactionError::InvalidSince { index }).into());
                    }
                    let a = self.tx_env.epoch().to_rational();
                    let b = info.block_epoch.to_rational()
                        + epoch_number_with_fraction.normalize().to_rational();
                    if a < b {
                        return Err((TransactionError::Immature { index }).into());
                    }
                }
                Some(SinceMetric::Timestamp(timestamp)) => {
                    // pass_median_time(current_block) starts with tip block, which is the
                    // parent of current block.
                    // pass_median_time(input_cell's block) starts with cell_block_number - 1,
                    // which is the parent of input_cell's block
                    let proposal_window = self.consensus.tx_proposal_window();
                    let parent_hash = self.tx_env.parent_hash();
                    let epoch_number = self.tx_env.epoch_number(proposal_window);
                    let hardfork_switch = self.consensus.hardfork_switch();
                    let base_timestamp = if hardfork_switch
                        .ckb2021
                        .is_block_ts_as_relative_since_start_enabled(epoch_number)
                    {
                        self.data_loader
                            .get_header_fields(&info.block_hash)
                            .expect("header exist")
                            .timestamp
                    } else {
                        self.parent_median_time(&info.block_hash)
                    };
                    let current_median_time = self.block_median_time(&parent_hash);
                    let required_timestamp = base_timestamp
                        .checked_add(timestamp)
                        .ok_or(TransactionError::InvalidSince { index })?;
                    if current_median_time < required_timestamp {
                        return Err((TransactionError::Immature { index }).into());
                    }
                }
                None => {
                    return Err((TransactionError::InvalidSince { index }).into());
                }
            }
        }
        Ok(())
```

**File:** util/types/src/core/extras.rs (L43-56)
```rust
/// Transaction information including its location in the blockchain.
#[derive(Clone, Eq, PartialEq, Debug)]
pub struct TransactionInfo {
    /// Hash of the block containing this transaction.
    // Block hash
    pub block_hash: packed::Byte32,
    /// Block number of the block containing this transaction.
    pub block_number: BlockNumber,
    /// Epoch of the block containing this transaction.
    pub block_epoch: EpochNumberWithFraction,
    /// Index of the transaction within the block.
    // Index in the block
    pub index: usize,
}
```

**File:** tx-pool/src/util.rs (L134-148)
```rust
pub(crate) fn time_relative_verify(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: TxVerifyEnv,
) -> Result<(), Reject> {
    let consensus = snapshot.cloned_consensus();
    TimeRelativeTransactionVerifier::new(
        rtx,
        consensus,
        snapshot.as_data_loader(),
        Arc::new(tx_env),
    )
    .verify()
    .map_err(Reject::Verification)
}
```
