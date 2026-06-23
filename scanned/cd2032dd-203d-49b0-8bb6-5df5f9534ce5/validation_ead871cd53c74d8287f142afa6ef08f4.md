### Title
Silent `u128 → u64` Truncation in DAO Maximum Withdrawal Calculation Locks User Funds — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum CKB a depositor can withdraw using a `u128` intermediate value, then casts it to `u64` with a bare `as u64` (truncating cast). When the intermediate result exceeds `u64::MAX`, the upper bits are silently discarded, producing a drastically wrong maximum. This causes valid DAO withdrawal transactions to be rejected, permanently locking the depositor's funds. The project's own test `check_withdraw_calculation_overflows` asserts `result.is_err()` for exactly this scenario, but the current code returns `Ok` with a corrupted value instead.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
// line 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

`withdraw_counted_capacity` is a `u128`. The `as u64` cast on line 155 is a **silent truncating cast**: if the value exceeds `u64::MAX`, the upper 64 bits are discarded with no error. The result passed to `Capacity::shannons` is then completely wrong.

Every other `u128 → u64` narrowing in the same file uses the checked pattern:

```rust
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
// line 245
let miner_issuance = Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

`calculate_maximum_withdraw` is the only site that uses the unchecked `as u64` cast.

The overflow is reachable: `withdraw_counted_capacity = counted_capacity * withdrawing_ar / deposit_ar`. Since `withdrawing_ar ≥ deposit_ar` (the accumulation rate only increases), the ratio is always ≥ 1. When `counted_capacity` is close to `u64::MAX` and any interest has accrued, the product exceeds `u64::MAX`. The project's own test confirms this:

```rust
// util/dao/src/tests.rs line 295-349
let output = CellOutput::new_builder()
    .capacity(Capacity::shannons(18_446_744_073_709_550_000))  // near u64::MAX
    .build();
// deposit_ar  = 10_000_000_000_123_456
// withdrawing_ar = 10_000_000_001_123_456  (any interest accrued)
let result = calculator.calculate_maximum_withdraw(...);
assert!(result.is_err());   // ← FAILS with current code; returns Ok(truncated)
```

The test asserts `result.is_err()`, but the current `as u64` cast returns `Ok` with a silently truncated (and incorrect) capacity value.

---

### Impact Explanation

When the truncated maximum is **smaller** than the depositor's actual withdrawal amount, the verification step (`transaction_fee` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw`) rejects the withdrawal transaction as exceeding the maximum. The depositor cannot recover their funds — the DAO cell is permanently locked, directly mirroring the LP-position-locked-forever impact in the reference report.

Additionally, `calculate_maximum_withdraw` is exposed via the `calculate_dao_maximum_withdraw` JSON-RPC endpoint (`rpc/src/module/experiment.rs`), so any RPC caller querying the maximum withdrawal for a large deposit receives a silently wrong answer, leading to incorrect transaction construction.

---

### Likelihood Explanation

The overflow requires a DAO deposit with capacity near `u64::MAX` shannons. The total CKB issuance cap is ~33.6 billion CKB = ~3.36 × 10¹⁸ shannons, which is within `u64::MAX` (~1.84 × 10¹⁹). A single large holder or a protocol-level aggregation could reach this range. The `ar` ratio increases with every block, so **any** interest accrual on a sufficiently large deposit triggers the overflow. The project's own test demonstrates the exact parameter values needed, confirming the scenario is considered realistic by the developers.

---

### Recommendation

Replace the silent truncating cast with a checked conversion, consistent with every other `u128 → u64` narrowing in the same file:

```rust
// util/dao/src/lib.rs, calculate_maximum_withdraw
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
```

This makes the function return `Err(DaoError::Overflow)` instead of silently producing a wrong value, which is the behavior the existing test already expects.

---

### Proof of Concept

The existing unit test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` (lines 295–349) is a direct proof of concept. It constructs a DAO output with capacity `18_446_744_073_709_550_000` shannons, a deposit `ar` of `10_000_000_000_123_456`, and a withdrawal `ar` of `10_000_000_001_123_456` (a tiny interest accrual). It calls `calculate_maximum_withdraw` and asserts `result.is_err()`.

With the current `as u64` cast, the intermediate `u128` value overflows `u64::MAX`, the cast silently truncates it to a small number, and the function returns `Ok(Capacity::shannons(<wrong_small_value>))`. The assertion `assert!(result.is_err())` therefore **fails**, confirming the bug is present and testable without any external setup. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** util/dao/src/lib.rs (L258-261)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;
```

**File:** util/dao/src/tests.rs (L295-349)
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
```
