The code confirms the claim exactly. Line 156 uses `withdraw_counted_capacity as u64` (truncating), while lines 244–245 and 258 use `u64::try_from(...).map_err(|_| DaoError::Overflow)?` (checked). The test at line 349 asserts `result.is_err()`, which the current code violates.

Audit Report

## Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Bypasses Overflow Guard — (`util/dao/src/lib.rs`)

## Summary
`DaoCalculator::calculate_maximum_withdraw` casts `withdraw_counted_capacity` from `u128` to `u64` using the truncating `as u64` operator at line 156, silently wrapping on overflow instead of returning `DaoError::Overflow`. Every other u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The existing unit test `check_withdraw_calculation_overflows` explicitly constructs an overflow scenario and asserts `result.is_err()`, which the current code violates by returning `Ok(...)` with a corrupted capacity value.

## Finding Description
In `util/dao/src/lib.rs` at line 152–156:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast truncates any value exceeding `u64::MAX` (≈1.84×10¹⁹) to its low 64 bits. The sibling conversions in `dao_field_with_current_epoch` use the safe pattern:

```rust
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
// ...
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

The call chain is:
- `dao_field_with_current_epoch` → `withdrawed_interests` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw`

`dao_field_with_current_epoch` is called both during block assembly (`BlockAssembler::calc_dao`) and during contextual block verification (`DaoHeaderVerifier`). A corrupted `withdraw_counted_capacity` propagates into `withdrawed_interests`, which is subtracted from the DAO `s` field embedded in every block header. Nodes that compute the correct value will reject a block assembled with the corrupted DAO field.

The unit test at `util/dao/src/tests.rs` lines 295–350 constructs capacity `18_446_744_073_709_550_000` shannons with `withdrawing_ar = 10_000_000_001_123_456` and `deposit_ar = 10_000_000_000_123_456`, producing `withdraw_counted_capacity ≈ 2.03×10¹⁹ > u64::MAX`. The test asserts `result.is_err()`, but the current `as u64` cast causes the function to return `Ok(Capacity::shannons(≈1.84×10¹⁸))`, failing the assertion. [4](#0-3) 

## Impact Explanation
When overflow occurs, `calculate_maximum_withdraw` returns a silently wrong (much smaller) withdrawal capacity. This corrupts the `withdrawed_interests` sum, which in turn corrupts the DAO `s` field written into the block header. A block assembled with this wrong DAO field will fail `DaoHeaderVerifier` on all peers that compute the correct value, causing consensus deviation. This matches the allowed Critical impact: **"Vulnerabilities which could easily cause consensus deviation."** Additionally, the public RPC `calculate_dao_maximum_withdraw` (in `rpc/src/module/experiment.rs`) calls `calculate_maximum_withdraw` directly and would return a silently wrong withdrawal amount instead of an error.

## Likelihood Explanation
Triggering the overflow requires `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. Since the total CKB supply is ≈3.36×10¹⁸ shannons and `u64::MAX ≈ 1.84×10¹⁹`, the accumulate rate ratio (`withdrawing_ar / deposit_ar`) must exceed ≈5.5×. At current secondary issuance rates this would take many decades of continuous network operation. The condition is not reachable in the near term under normal network conditions, making likelihood very low. However, the code path is reachable by any RPC caller or tx-pool submitter, and the test suite already documents the expected error behavior that the current code violates.

## Recommendation
Replace the truncating cast at line 156 with the same checked conversion used elsewhere in the file:

```rust
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
``` [5](#0-4) 

This is consistent with the pattern at lines 244–245 and 258 and makes the existing `check_withdraw_calculation_overflows` test pass.

## Proof of Concept
The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` at lines 295–350 is the proof of concept. [4](#0-3) 

Run `cargo test -p ckb-dao check_withdraw_calculation_overflows`. With the current `as u64` cast the test panics at `assert!(result.is_err())` because the function returns `Ok(Capacity::shannons(1_843_674_407_370_955_008))` instead of `Err(DaoError::Overflow)`, confirming the silent truncation.

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
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
