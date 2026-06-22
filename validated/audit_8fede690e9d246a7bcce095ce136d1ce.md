### Title
`estimate_fee_rate` Fallback Uses Full Block Byte Limit Instead of Actual Transaction Space, Returning Systematically Underestimated Fee Rates — (`tx-pool/src/pool.rs`)

---

### Summary

The fallback implementation of `estimate_fee_rate` in `tx-pool/src/pool.rs` simulates block packing using `consensus.max_block_bytes()` as the full available space per block. However, the actual block assembler in `tx-pool/src/block_assembler/mod.rs` subtracts a `basic_size` overhead (cellbase, uncle blocks, proposal short IDs, and extension) before computing the real transaction space limit. This discrepancy means the fee rate estimator consistently underestimates the required fee rate, analogous to how `V3Vault::maxDeposit` returned values that did not account for the `dailyLendIncreaseLimitLeft` constraint.

---

### Finding Description

**Root cause — fallback `estimate_fee_rate` in `tx-pool/src/pool.rs`:** [1](#0-0) 

The fallback calls `pool_map.estimate_fee_rate` with the raw consensus block byte limit:

```rust
let fee_rate = self.pool_map.estimate_fee_rate(
    (target_to_be_committed - self.snapshot.consensus().tx_proposal_window().closest()) as usize,
    self.snapshot.consensus().max_block_bytes() as usize,   // ← full block limit
    self.snapshot.consensus().max_block_cycles(),
    self.config.min_fee_rate,
);
```

**Actual block assembly in `tx-pool/src/block_assembler/mod.rs`:** [2](#0-1) 

The assembler correctly subtracts the overhead before selecting transactions:

```rust
let basic_size = Self::basic_block_size(
    current_template.cellbase.data(),
    uncles,
    proposals.iter(),
    current_template.extension.clone(),
);
let txs_size_limit = max_block_bytes
    .checked_sub(basic_size)          // ← overhead subtracted
    .ok_or(BlockAssemblerError::Overflow)?;
```

**`pool_map.estimate_fee_rate` simulation loop:** [3](#0-2) 

The loop accumulates `current_block_bytes` against `max_block_bytes` (the full limit), so it simulates blocks that are larger than what the assembler actually produces. Each simulated block absorbs more transactions than a real block would, causing the returned fee rate to be lower than what is actually needed to be included within the target number of blocks.

**The `TRANSACTION_SIZE_LIMIT` constant and `tx_size_limit` field in `TxPoolInfo`:** [4](#0-3) [5](#0-4) 

The `tx_pool_info` RPC exposes `tx_size_limit` (a static 512 KB cap per individual transaction) and `max_tx_pool_size`, but neither field accounts for the per-block overhead that reduces the effective transaction space. A caller computing available capacity from these fields faces the same mismatch.

**The `estimate_fee_rate` RPC entry point:** [6](#0-5) 

Any unprivileged RPC caller invoking `estimate_fee_rate` receives the underestimated value.

---

### Impact Explanation

A transaction submitted with the fee rate returned by `estimate_fee_rate` (fallback path) will be accepted into the pool (it clears `min_fee_rate`) but will compete for a smaller real block space than the estimator assumed. The transaction therefore takes more blocks to confirm than the caller's `target_to_be_committed` parameter requested. In time-sensitive use cases (e.g., DAO withdrawal before an epoch boundary, or any protocol that depends on timely on-chain settlement), this systematic underestimation causes missed deadlines and economic loss without any on-chain revert to alert the user.

The overhead omitted by the estimator includes:
- Cellbase transaction (hundreds of bytes)
- Up to `max_block_proposals_limit` (1 500) proposal short IDs × 10 bytes = up to 15 000 bytes
- Extension field (32–96 bytes per RFC 0044)

With `max_block_bytes ≈ 597 000` bytes (as shown in the RPC example response), the real transaction space is roughly `597 000 − 15 000 − ~300 ≈ 581 700` bytes — about 2.5 % less than what the estimator assumes per block.

---

### Likelihood Explanation

The fallback path is activated whenever the primary fee estimator (`ConfirmationFraction` or `WeightUnitsFlow`) returns `Error::LackData` or `Error::NotReady` — which is the common case immediately after node startup, after IBD, or on a lightly-used network. Any RPC caller (wallet, exchange, dApp) that calls `estimate_fee_rate` during these periods receives the underestimated value. No special privileges or attacker-controlled state are required; the discrepancy is structural and reproducible.

---

### Recommendation

In `tx-pool/src/pool.rs`, the fallback `estimate_fee_rate` should subtract a representative `basic_block_overhead` from `max_block_bytes` before passing it to `pool_map.estimate_fee_rate`. A conservative constant (e.g., the maximum proposal overhead `max_block_proposals_limit * ProposalShortId::serialized_size()` plus a fixed cellbase/extension budget) should be used, mirroring the logic in `BlockAssembler::basic_block_size`. Alternatively, the block assembler's current `basic_size` can be exposed and reused by the estimator.

The `WeightUnitsFlow` algorithm already applies an 85% factor (`MAX_BLOCK_BYTES * 85 / 100`) as a heuristic correction; the fallback path should adopt a similar or more precise adjustment.

---

### Proof of Concept

1. Start a fresh CKB node (IBD just completed; `WeightUnitsFlow` returns `LackData`).
2. Call `estimate_fee_rate` via RPC — the fallback path is taken.
3. The returned fee rate `F` is computed assuming each simulated block can hold `max_block_bytes` bytes of transactions.
4. Submit a transaction with fee rate `F`.
5. Observe that the block assembler selects transactions against `max_block_bytes - basic_size < max_block_bytes`, so the pool is effectively more congested than the estimator modelled.
6. The transaction is not included within the requested `target_to_be_committed` blocks; it requires additional blocks to confirm.

The discrepancy is directly visible by comparing the `txs_size_limit` logged by `BlockAssembler::update_full` against the `max_block_bytes` value passed to `pool_map.estimate_fee_rate`.

### Citations

**File:** tx-pool/src/pool.rs (L557-572)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        target_to_be_committed: BlockNumber,
    ) -> Result<FeeRate, FeeEstimatorError> {
        if !(3..=131).contains(&target_to_be_committed) {
            return Err(FeeEstimatorError::NoProperFeeRate);
        }
        let fee_rate = self.pool_map.estimate_fee_rate(
            (target_to_be_committed - self.snapshot.consensus().tx_proposal_window().closest())
                as usize,
            self.snapshot.consensus().max_block_bytes() as usize,
            self.snapshot.consensus().max_block_cycles(),
            self.config.min_fee_rate,
        );
        Ok(fee_rate)
    }
```

**File:** tx-pool/src/block_assembler/mod.rs (L199-213)
```rust
            let basic_size = Self::basic_block_size(
                current_template.cellbase.data(),
                uncles,
                proposals.iter(),
                current_template.extension.clone(),
            );

            let txs_size_limit = max_block_bytes
                .checked_sub(basic_size)
                .ok_or(BlockAssemblerError::Overflow)?;

            let max_block_cycles = consensus.max_block_cycles();
            let (txs, _txs_size, _cycles) =
                tx_pool_reader.package_txs(max_block_cycles, txs_size_limit);
            (proposals, txs, basic_size)
```

**File:** tx-pool/src/component/pool_map.rs (L334-359)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
    }
```

**File:** util/types/src/core/tx_pool.rs (L306-309)
```rust
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

**File:** util/jsonrpc-types/src/pool.rs (L53-60)
```rust
    /// Limiting transactions to tx_size_limit
    ///
    /// Transactions with a large size close to the block size limit may not be packaged,
    /// because the block header and cellbase are occupied,
    /// so the tx-pool is limited to accepting transaction up to tx_size_limit.
    pub tx_size_limit: Uint64,
    /// Total limit on the size of transactions in the tx-pool
    pub max_tx_pool_size: Uint64,
```

**File:** rpc/src/module/experiment.rs (L301-315)
```rust
    fn estimate_fee_rate(
        &self,
        estimate_mode: Option<EstimateMode>,
        enable_fallback: Option<bool>,
    ) -> Result<Uint64> {
        let estimate_mode = estimate_mode.unwrap_or_default();
        let enable_fallback = enable_fallback.unwrap_or(true);
        self.shared
            .tx_pool_controller()
            .estimate_fee_rate(estimate_mode.into(), enable_fallback)
            .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?
            .map_err(RPCError::from_any_error)
            .map(core::FeeRate::as_u64)
            .map(Into::into)
    }
```
