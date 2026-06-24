The code is confirmed. Let me verify the math and call chain before rendering a verdict.

Audit Report

## Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` — (`util/dao/src/lib.rs`)

## Summary

`DaoCalculator::calculate_maximum_withdraw` uses a truncating `as u64` cast at line 156 when narrowing `withdraw_counted_capacity` from `u128` to `u64`. Every other equivalent narrowing in the same file (`secondary_block_reward` at line 204, `dao_field_with_current_epoch` at lines 244–245 and 258) uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the intermediate product overflows `u64`, the function silently returns a corrupted withdrawal capacity instead of propagating `DaoError::Overflow`. The existing unit test `check_withdraw_calculation_overflows` asserts `result.is_err()` for exactly this scenario; with the current cast the function returns `Ok(...)`, so the test fails, directly confirming the bug.

## Finding Description

**Root cause — line 156:**
```rust
// util/dao/src/lib.rs  lines 152–156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

**Contrast with the safe pattern used everywhere else in the same file:**
```rust
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
// lines 244–245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

**Confirmed call chain to consensus-critical path:**

`calculate_maximum_withdraw` (line 156) is called by `transaction_maximum_withdraw`, which is called by `withdrawed_interests` (line 317), which is called by `dao_field_with_current_epoch` (line 222). `dao_field_with_current_epoch` is invoked during both block assembly (`BlockAssembler::calc_dao`) and contextual block verification (`DaoHeaderVerifier`). [5](#0-4) 

**Why existing checks are insufficient:** `safe_add` at line 156 only guards against overflow in the *addition* of `occupied_capacity`; it does not detect that `withdraw_counted_capacity` was already silently truncated before being passed to `Capacity::shannons`. The truncated value is a valid `u64`, so `safe_add` succeeds and `Ok(corrupted_capacity)` is returned.

## Impact Explanation

A corrupted `withdraw_counted_capacity` propagates into `withdrawed_interests`, which is subtracted from the running DAO `s` field packed into every block header. A block assembled with this wrong DAO field will be rejected by any peer that computes the correct value, causing a **consensus split**. This matches the Critical allowed impact: *"Vulnerabilities which could easily cause consensus deviation."* The public RPC `calculate_dao_maximum_withdraw` also calls `calculate_maximum_withdraw` directly and would silently return a wrong (much smaller) withdrawal amount to callers.

## Likelihood Explanation

Triggering the overflow requires a single deposited cell where `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. Since the total CKB supply is ~3.36 × 10¹⁸ shannons and `u64::MAX ≈ 1.84 × 10¹⁹`, the accumulate-rate ratio `withdrawing_ar / deposit_ar` must exceed ~5.5×. At current secondary issuance rates this would take many decades of continuous accumulation. Likelihood is therefore very low under normal network conditions. However, the code path is reachable by any unprivileged RPC caller or transaction submitter, and the test suite already documents the expected error behaviour that the current code violates.

## Recommendation

Replace the truncating cast with the checked conversion already used in every sibling function:

```rust
// util/dao/src/lib.rs  lines 155–156
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
```

This is a one-line change that matches the established pattern at lines 204, 244–245, and 258, and makes the existing `check_withdraw_calculation_overflows` test pass. [6](#0-5) 

## Proof of Concept

The existing unit test at `util/dao/src/tests.rs` lines 295–350 is a self-contained PoC. [7](#0-6) 

It constructs a cell with `capacity = 18_446_744_073_709_550_000` shannons, `deposit_ar = 10_000_000_000_123_456`, `withdrawing_ar = 10_000_000_001_123_456`. The product `18_446_744_073_709_550_000 × 10_000_000_001_123_456 / 10_000_000_000_123_456 > u64::MAX`, so the test asserts `result.is_err()`. With the current `as u64` cast the function returns `Ok(Capacity::shannons(truncated_value))` — the assertion fails, directly demonstrating the silent truncation. Run with:

```
cargo test -p ckb-dao check_withdraw_calculation_overflows
```

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L204-204)
```rust
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L258-258)
```rust
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
