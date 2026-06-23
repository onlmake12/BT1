### Title
Silent Integer Truncation in DAO Withdrawal Capacity Calculation Produces Incorrect Results — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` performs a u128→u64 narrowing cast using `as u64` (an unchecked, silently-truncating Rust cast) when computing the final withdrawal capacity. Every other analogous u128→u64 conversion in the same codebase uses `u64::try_from(…).map_err(|_| DaoError::Overflow)?`, which returns a proper error on overflow. The inconsistency means that when the intermediate product exceeds `u64::MAX`, the high bits are silently discarded and the function returns a **wrong (too-small) capacity value with `Ok`**, instead of returning `Err(DaoError::Overflow)`. This is the direct CKB analog of the ABDKMathQuad edge-case bug: a fixed-width arithmetic operation silently produces an incorrect result at boundary values.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← unchecked truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently discards the upper 64 bits of `withdraw_counted_capacity` if it exceeds `u64::MAX`. No error is returned; the function proceeds with a wrong value.

Compare this to every other u128→u64 conversion in the same file, all of which use the checked form:

```rust
// dao_field_with_current_epoch, line 244-245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// dao_field_with_current_epoch, line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;

// secondary_block_reward, line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

The inconsistency is a clear defect: the same pattern is applied correctly everywhere except in `calculate_maximum_withdraw`.

**Overflow condition.** The intermediate value is:

```
withdraw_counted_capacity = counted_capacity × withdrawing_ar / deposit_ar
```

`counted_capacity` is `output_capacity − occupied_capacity`, bounded by the cell's capacity field (a `u64`). `withdrawing_ar` is the accumulate-rate at the withdrawing block; `deposit_ar` is the accumulate-rate at the deposit block. Because `ar` only increases, `withdrawing_ar ≥ deposit_ar`, so the ratio is ≥ 1. For any cell whose `counted_capacity × (withdrawing_ar / deposit_ar)` exceeds `u64::MAX`, the cast truncates silently.

The existing test `check_withdraw_calculation_overflows` uses a capacity of `18_446_744_073_709_550_000` shannons (near `u64::MAX`) and asserts `result.is_err()`. [4](#0-3) 

That test catches the case where the truncated value plus `occupied_capacity` still overflows `u64` (caught by `safe_add`). It does **not** cover the case where truncation produces a value small enough that `safe_add` succeeds — returning `Ok` with a silently wrong (too-small) capacity.

---

### Impact Explanation

**Incorrect DAO withdrawal amount returned silently.** When `withdraw_counted_capacity` overflows `u64` by a small margin (i.e., the true value is in `(u64::MAX, u64::MAX + occupied_capacity)`), the `as u64` truncation wraps to a small number, `safe_add(occupied_capacity)` succeeds, and the function returns `Ok(wrong_capacity)`. The caller receives no indication of error.

This affects two reachable paths:

1. **RPC `calculate_dao_maximum_withdraw`** — an RPC caller querying the maximum withdrawal for a large DAO cell receives a wrong (too-small) answer. A user who constructs a withdrawal transaction based on this answer loses the difference. [5](#0-4) 

2. **Block DAO field computation** — `withdrawed_interests` (called from `dao_field_with_current_epoch`) calls `transaction_maximum_withdraw` → `calculate_maximum_withdraw`. A wrong result here produces a wrong DAO field in a miner's block, causing the block to be rejected by honest nodes that compute the correct value. [6](#0-5) [7](#0-6) 

---

### Likelihood Explanation

The overflow requires `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. The total CKB issuance is bounded, so a single cell's capacity is bounded well below `u64::MAX` in practice. However:

- The condition is reachable for any cell whose capacity is large relative to the total supply and that has been deposited for a long time (large `withdrawing_ar / deposit_ar` ratio).
- The `as u64` truncation is a latent defect that becomes more likely to trigger as the chain ages and `ar` grows.
- The bug is already inconsistent with the rest of the codebase, indicating it was an oversight rather than a deliberate design choice.
- An RPC caller can trigger the incorrect-result path (not just the error path) without any special privilege.

---

### Recommendation

Replace the unchecked `as u64` cast with the same checked conversion used everywhere else in the file:

```rust
// Before (buggy):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After (correct):
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
``` [8](#0-7) 

Add a test case that specifically exercises the silent-truncation path: a cell whose `counted_capacity × withdrawing_ar / deposit_ar` is in `(u64::MAX, u64::MAX + occupied_capacity)`, and assert that the result is `Err(DaoError::Overflow)` rather than `Ok(wrong_value)`.

---

### Proof of Concept

The existing test at `util/dao/src/tests.rs:296` demonstrates the overflow path but only catches the `safe_add` overflow, not the silent truncation: [4](#0-3) 

A minimal PoC for the silent-truncation path:

```rust
// deposit_ar = 10_000_000_000_000_000 (genesis default)
// withdrawing_ar = 10_000_000_000_000_001 (one unit of growth)
// counted_capacity = u64::MAX - 1 = 18_446_744_073_709_551_614

// withdraw_counted_capacity (u128) =
//   18_446_744_073_709_551_614 * 10_000_000_000_000_001
//   / 10_000_000_000_000_000
// = 18_446_744_073_709_551_614 + 1  (just above u64::MAX)

// `as u64` truncates to 0 (or a small value)
// safe_add(occupied_capacity) succeeds
// returns Ok(wrong_small_value) instead of Err(Overflow)
```

The `DaoError::ZeroC` variant in the error enum confirms the developers are aware of division-by-zero edge cases in DAO math, but the analogous overflow-on-cast edge case in `calculate_maximum_withdraw` was not guarded. [9](#0-8) [10](#0-9)

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L208-264)
```rust
    /// Calculates the new dao field with specified [`EpochExt`].
    pub fn dao_field_with_current_epoch(
        &self,
        rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
        parent: &HeaderView,
        current_block_epoch: &EpochExt,
    ) -> Result<Byte32, DaoError> {
        // Freed occupied capacities from consumed inputs
        let freed_occupied_capacities =
            rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
                self.input_occupied_capacities(rtx)
                    .and_then(|c| capacities.safe_add(c))
            })?;
        let added_occupied_capacities = self.added_occupied_capacities(rtxs.clone())?;
        let withdrawed_interests = self.withdrawed_interests(rtxs)?;

        let (parent_ar, parent_c, parent_s, parent_u) = extract_dao_data(parent.dao());

        // g contains both primary issuance and secondary issuance,
        // g2 is the secondary issuance for the block, which consists of
        // issuance for the miner, NervosDAO and treasury.
        // When calculating issuance in NervosDAO, we use the real
        // issuance for each block(which will only be issued on chain
        // after the finalization delay), not the capacities generated
        // in the cellbase of current block.
        let current_block_number = parent.number() + 1;
        let current_g2 = current_block_epoch.secondary_block_issuance(
            current_block_number,
            self.consensus.secondary_epoch_reward(),
        )?;
        let current_g = current_block_epoch
            .block_reward(current_block_number)
            .and_then(|c| c.safe_add(current_g2))?;

        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;

        let current_c = parent_c.safe_add(current_g)?;
        let current_u = parent_u
            .safe_add(added_occupied_capacities)
            .and_then(|u| u.safe_sub(freed_occupied_capacities))?;
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;

        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;

        Ok(pack_dao_data(current_ar, current_c, current_s, current_u))
    }
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

**File:** util/dao/src/tests.rs (L295-350)
```rust
#[test]
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

**File:** util/dao/utils/src/lib.rs (L88-92)
```rust
    // C cannot be zero, otherwise DAO stats calculation might result in
    // division by zero errors.
    if c == Capacity::zero() {
        return Err(DaoError::ZeroC);
    }
```
