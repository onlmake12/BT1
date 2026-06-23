### Title
Silent Arithmetic Truncation in DAO Withdrawal Capacity Calculation — (`File: util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` performs a `u128 → u64` cast via `as u64` (a silent truncating cast) when computing the final withdrawal capacity. Every other analogous intermediate result in the same codebase uses `u64::try_from(…).map_err(|_| DaoError::Overflow)?` to detect and surface overflow. The inconsistency means that if the intermediate `u128` product exceeds `u64::MAX`, the result is silently truncated to its lower 64 bits, producing a wrong (too-small) withdrawal amount that propagates into both the tx-pool fee check and the consensus-critical DAO field computation.

### Finding Description

In `calculate_maximum_withdraw`:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The same file's `dao_field_with_current_epoch` performs the identical `u128` intermediate calculation but correctly uses `u64::try_from(…)`:

```rust
// util/dao/src/lib.rs  lines 242-245
let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
    / u128::from(parent_c.as_u64());
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
``` [2](#0-1) 

```rust
// util/dao/src/lib.rs  lines 256-258
let ar_increase128 =
    u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [3](#0-2) 

The overflow condition is: `counted_capacity × withdrawing_ar > u64::MAX × deposit_ar`. Because `ar` starts at `DEFAULT_GENESIS_ACCUMULATE_RATE = 10_000_000_000_000_000` and grows by `ar × g2 / c` per block, the ratio `withdrawing_ar / deposit_ar` is always ≥ 1 and grows slowly on mainnet. However, on a chain with high secondary issuance relative to total capacity (custom chain spec, devnet, or far-future mainnet), the ratio can grow large enough to push the product past `u64::MAX`. [4](#0-3) 

### Impact Explanation

`calculate_maximum_withdraw` feeds into two critical paths:

1. **Tx-pool fee check** (`check_tx_fee` → `transaction_fee` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw`): a truncated `withdraw_capacity` that falls below the transaction's `outputs_capacity` produces a negative fee, causing the DAO withdrawal transaction to be rejected with `Reject::Malformed`. Legitimate DAO depositors cannot withdraw their funds. [5](#0-4) 

2. **Consensus DAO field verification** (`dao_field_with_current_epoch` → `withdrawed_interests` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw`): a truncated value makes `withdrawed_interests` too small, inflating `current_s` in the packed DAO field. Both the miner and the verifier run the same truncated code, so the corrupted DAO field is accepted by consensus — permanently corrupting the NervosDAO savings accumulator `s`. [6](#0-5) 

3. **`calculate_dao_maximum_withdraw` RPC**: returns a wrong (too-small) value to callers, causing wallets and tooling to construct withdrawal transactions with insufficient output capacity. [7](#0-6) 

### Likelihood Explanation

On mainnet the `ar` ratio grows by roughly `4 000` per block (secondary issuance ≈ 1.344 × 10⁹ shannons, total capacity ≈ 3.36 × 10¹⁸ shannons), so overflow requires an astronomically long time under current parameters. However:

- Any custom chain spec with a higher `secondary_epoch_reward` or lower genesis capacity reaches the overflow threshold orders of magnitude sooner.
- A transaction sender can freely choose which deposit header and withdrawing header to reference via `header_deps`, selecting the pair that maximises `withdrawing_ar / deposit_ar`.
- The `calculate_dao_maximum_withdraw` RPC is callable by any unprivileged RPC user and will silently return wrong values whenever the condition is met. [8](#0-7) 

### Recommendation

Replace the silent truncating cast with the same checked conversion used everywhere else in the file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64)

// After (consistent with the rest of the file):
Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
)
```

This makes overflow observable and returns `DaoError::Overflow` to callers instead of silently corrupting the result, consistent with the existing `DaoError::Overflow` handling already present in `dao_field_with_current_epoch` and `secondary_block_reward`. [9](#0-8) 

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` already demonstrates the overflow scenario for `calculate_maximum_withdraw` — it asserts `result.is_err()`. However, with the current `as u64` cast the function does **not** return an error; it silently returns a truncated value, meaning the test would fail if the cell capacity were set high enough to trigger the u128 overflow:

```rust
// util/dao/src/tests.rs  lines 296-350
fn check_withdraw_calculation_overflows() {
    let output = CellOutput::new_builder()
        .capacity(Capacity::shannons(18_446_744_073_709_550_000))  // near u64::MAX
        ...
    assert!(result.is_err());   // passes only because safe_sub catches a different overflow
}
``` [10](#0-9) 

A targeted PoC: set `counted_capacity = u64::MAX`, `withdrawing_ar = 2 × deposit_ar`. Then `withdraw_counted_capacity = u64::MAX × 2 = 0x1_FFFF_FFFF_FFFF_FFFE` (u128). The `as u64` cast yields `0xFFFF_FFFF_FFFF_FFFE` — off by exactly `u64::MAX`, silently accepted. With `u64::try_from` this would return `DaoError::Overflow`. [11](#0-10)

### Citations

**File:** util/dao/src/lib.rs (L146-158)
```rust
        let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
        let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());

        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

        Ok(withdraw_capacity)
```

**File:** util/dao/src/lib.rs (L242-245)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L312-333)
```rust
    fn withdrawed_interests(
        &self,
        mut rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
    ) -> Result<Capacity, DaoError> {
        let maximum_withdraws = rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
            self.transaction_maximum_withdraw(rtx)
                .and_then(|c| capacities.safe_add(c).map_err(Into::into))
        })?;
        let input_capacities = rtxs.try_fold(Capacity::zero(), |capacities, rtx| {
            let tx_input_capacities = rtx.resolved_inputs.iter().try_fold(
                Capacity::zero(),
                |tx_capacities, cell_meta| {
                    let output_capacity: Capacity = cell_meta.cell_output.capacity().into();
                    tx_capacities.safe_add(output_capacity)
                },
            )?;
            capacities.safe_add(tx_input_capacities)
        })?;
        maximum_withdraws
            .safe_sub(input_capacities)
            .map_err(Into::into)
    }
```

**File:** util/dao/utils/src/lib.rs (L17-17)
```rust
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
```

**File:** tx-pool/src/util.rs (L28-53)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
```

**File:** rpc/src/module/experiment.rs (L235-298)
```rust
    fn calculate_dao_maximum_withdraw(
        &self,
        out_point: OutPoint,
        kind: DaoWithdrawingCalculationKind,
    ) -> Result<Capacity> {
        let snapshot: &Snapshot = &self.shared.snapshot();
        let consensus = snapshot.consensus();
        let out_point: packed::OutPoint = out_point.into();
        let data_loader = snapshot.borrow_as_data_loader();
        let calculator = DaoCalculator::new(consensus, &data_loader);
        match kind {
            DaoWithdrawingCalculationKind::WithdrawingHeaderHash(withdrawing_header_hash) => {
                let (tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output = tx
                    .outputs()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output_data = tx
                    .outputs_data()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
            }
            DaoWithdrawingCalculationKind::WithdrawingOutPoint(withdrawing_out_point) => {
                let (_tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                let withdrawing_out_point: packed::OutPoint = withdrawing_out_point.into();
                let (withdrawing_tx, withdrawing_header_hash) = snapshot
                    .get_transaction(&withdrawing_out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;

                let output = withdrawing_tx
                    .outputs()
                    .get(withdrawing_out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;
                let output_data = withdrawing_tx
                    .outputs_data()
                    .get(withdrawing_out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash,
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
            }
        }
```

**File:** util/dao/utils/src/error.rs (L36-41)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
    /// ZeroC
    #[error("ZeroC")]
    ZeroC,
```

**File:** util/dao/src/tests.rs (L296-350)
```rust
fn check_withdraw_calculation_overflows() {
    let output = CellOutput::new_builder()
        .capacity(Capacity::shannons(18_446_744_073_709_550_000))
        .build();
    let tx = TransactionBuilder::default().output(output.clone()).build();
    let epoch = EpochNumberWithFraction::new(1, 100, 1000);
    let deposit_header = HeaderBuilder::default()
        .number(100)
        .epoch(epoch)
        .dao(pack_dao_data(
            10_000_000_000_123_456,
            Default::default(),
            Default::default(),
            Default::default(),
        ))
        .build();
    let deposit_block = BlockBuilder::default()
        .header(deposit_header)
        .transaction(tx)
        .build();

    let epoch = EpochNumberWithFraction::new(1, 200, 1000);
    let withdrawing_header = HeaderBuilder::default()
        .number(200)
        .epoch(epoch)
        .dao(pack_dao_data(
            10_000_000_001_123_456,
            Default::default(),
            Default::default(),
            Default::default(),
        ))
        .build();
    let withdrawing_block = BlockBuilder::default().header(withdrawing_header).build();

    let tmp_dir = TempDir::new().unwrap();
    let db = RocksDB::open_in(&tmp_dir, COLUMNS);
    let store = ChainDB::new(db, Default::default());
    let txn = store.begin_transaction();
    txn.insert_block(&deposit_block).unwrap();
    txn.attach_block(&deposit_block).unwrap();
    txn.insert_block(&withdrawing_block).unwrap();
    txn.attach_block(&withdrawing_block).unwrap();
    txn.commit().unwrap();

    let consensus = Consensus::default();
    let data_loader = store.borrow_as_data_loader();
    let calculator = DaoCalculator::new(&consensus, &data_loader);
    let result = calculator.calculate_maximum_withdraw(
        &output,
        Capacity::bytes(0).expect("should not overflow"),
        &deposit_block.hash(),
        &withdrawing_block.hash(),
    );
    assert!(result.is_err());
}
```
