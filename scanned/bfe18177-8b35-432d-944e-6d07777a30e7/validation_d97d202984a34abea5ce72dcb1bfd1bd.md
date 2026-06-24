Audit Report

## Title
Unchecked `as u64` Truncating Cast in DAO Withdrawal Capacity Calculation — (File: util/dao/src/lib.rs)

## Summary
`DaoCalculator::calculate_maximum_withdraw` computes `withdraw_counted_capacity` as a `u128` and converts it to `u64` via a bare `as u64` cast at line 156, silently discarding the upper 64 bits on overflow. Every other `u128 → u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The existing test `check_withdraw_calculation_overflows` asserts `result.is_err()` for an overflow scenario, but the silent cast causes the function to return `Ok(wrong_capacity)` instead, meaning the guard is absent and the test assertion fails.

## Finding Description
In `util/dao/src/lib.rs` at lines 152–156, the calculation is:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)  // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast is a Rust truncating cast — when `withdraw_counted_capacity > u64::MAX`, the high 64 bits are silently dropped and the function returns `Ok(corrupted_capacity)` with no error. This is the sole outlier; all other `u128 → u64` conversions in the file use the checked form:

- Line 204: `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?`
- Line 245: `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`
- Line 258: `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?` [2](#0-1) [3](#0-2) [4](#0-3) 

The corrupted capacity propagates through two consensus-critical paths:

1. `calculate_maximum_withdraw` → `transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch`: the wrong value is used to compute the `s` accumulator embedded in every block header's DAO field. A block produced with a truncated `s` value will be rejected by honest nodes that recompute the correct value, causing a consensus split. [5](#0-4) 

2. `calculate_maximum_withdraw` → `transaction_maximum_withdraw` → `transaction_fee`: the tx-pool computes `maximum_withdraw.safe_sub(outputs_capacity)` using the truncated value, producing a wrong (or underflowing) fee. [6](#0-5) 

## Impact Explanation
**Critical — Consensus Deviation.** A node that processes a DAO withdrawal transaction where `withdraw_counted_capacity` overflows `u64` will embed a wrong DAO field (`s` accumulator) in the block header. Honest nodes recomputing the DAO field from scratch will derive a different value and reject the block, causing a network-level consensus split. This matches the allowed critical impact: *"Vulnerabilities which could easily cause consensus deviation."*

## Likelihood Explanation
For overflow, `counted_capacity × (withdrawing_ar / deposit_ar) > u64::MAX`. The total CKB supply (~3.36 × 10¹⁸ shannons) is below `u64::MAX`, so overflow via a realistic single cell requires the AR ratio to exceed ~5.49×. AR grows monotonically with secondary issuance from its initial value of 10¹⁶; reaching 5.49× is a long-horizon mainnet condition. However, the test fixture already demonstrates a reachable overflow path using a near-`u64::MAX` capacity cell with only a modest AR increase, confirming the code path is exercisable and the guard is absent. The bug is also directly proven by the failing test assertion. [7](#0-6) 

## Recommendation
Replace the unchecked cast with the same checked conversion used everywhere else in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (safe, consistent with the rest of the file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
``` [8](#0-7) 

## Proof of Concept
The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` (lines 295–350) is the direct PoC:

- Cell capacity: `18_446_744_073_709_550_000` shannons (≈ `u64::MAX`)
- `deposit_ar = 10_000_000_000_123_456`, `withdrawing_ar = 10_000_000_001_123_456`
- `withdraw_counted_capacity ≈ 18_446_744_073_709_550_000 × 1.0000000001... > u64::MAX`

With the current `as u64` cast, the function returns `Ok(truncated_wrong_value)`. The test asserts `result.is_err()`, so the test **fails**, directly proving the missing overflow guard. Running `cargo test check_withdraw_calculation_overflows -p ckb-dao` reproduces the failure. [9](#0-8)

### Citations

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
    }
```

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
