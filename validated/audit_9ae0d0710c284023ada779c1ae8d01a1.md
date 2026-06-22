### Title
Silent `u64` Truncation in `calculate_maximum_withdraw` Returns Wrong Withdrawal Amount Instead of Error — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes a `u128` intermediate value and then casts it to `u64` with a bare `as u64`, which silently truncates on overflow. Every other analogous `u128 → u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The inconsistency means that when the intermediate result exceeds `u64::MAX`, the function silently returns a drastically wrong (truncated) withdrawal capacity instead of propagating `DaoError::Overflow`.

---

### Finding Description

In `calculate_maximum_withdraw`, the withdrawal capacity is computed as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast wraps silently. If `withdraw_counted_capacity > u64::MAX`, the low 64 bits are returned as a `Capacity`, producing a value that is billions of shannons smaller than the depositor is owed.

Every other `u128 → u64` narrowing in the same file uses the checked form:

```rust
// secondary_block_reward
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch (miner_issuance)
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)

// dao_field_with_current_epoch (ar_increase)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

Because `withdrawing_ar ≥ deposit_ar` always holds (the accumulation rate is monotonically non-decreasing), the ratio `withdrawing_ar / deposit_ar` grows over time. The overflow triggers when that ratio exceeds `u64::MAX / counted_capacity`.

The existing test `check_withdraw_calculation_overflows` (lines 296–349) constructs exactly this scenario with a near-`u64::MAX` output capacity and expects `result.is_err()`. With the `as u64` cast the function instead returns `Ok(Capacity::shannons(<truncated_value>))`, so the test fails — confirming the bug is present and detectable. [5](#0-4) 

---

### Impact Explanation

A DAO depositor who withdraws receives a silently wrong (far too small) capacity. The node accepts the transaction as valid because the arithmetic error is invisible to the verifier — it sees a well-formed `Capacity` value. The depositor loses the difference between the correct withdrawal and the truncated value. No panic or error is surfaced; the loss is silent.

---

### Likelihood Explanation

The total CKB issuance is approximately 3.36 × 10¹⁸ shannons, well below `u64::MAX` ≈ 1.84 × 10¹⁹ shannons. For a single cell holding the entire supply, the ratio `withdrawing_ar / deposit_ar` would need to exceed ~5.5× before overflow occurs. Given the slow growth of the secondary issuance accumulation rate, this would require centuries of chain operation. On present-day mainnet the overflow is not reachable in practice. The primary concrete impact is that the existing unit test `check_withdraw_calculation_overflows` fails, demonstrating the code does not behave as its authors intended.

---

### Recommendation

Replace the silent cast with the same checked pattern used everywhere else in the file:

```rust
// Before (silent truncation):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After (consistent with the rest of the file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [6](#0-5) 

---

### Proof of Concept

The repository already contains a failing test that demonstrates the bug:

```
util/dao/src/tests.rs::check_withdraw_calculation_overflows
``` [5](#0-4) 

The test sets:
- `output.capacity` = 18 446 744 073 709 550 000 shannons (≈ `u64::MAX`)
- `deposit_ar` = 10 000 000 000 123 456
- `withdrawing_ar` = 10 000 000 001 123 456

`withdraw_counted_capacity` (u128) ≈ 1.84 × 10¹⁹, which exceeds `u64::MAX`. With `as u64` the value wraps to a small number (~18 billion shannons), `safe_add` succeeds, and the function returns `Ok(...)`. The test asserts `result.is_err()` and therefore fails, proving the silent truncation is the root cause.

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
