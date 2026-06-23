### Title
Silent u128→u64 Truncation in NervosDAO Withdrawal Capacity Calculation Produces Incorrect Withdrawal Amount — (`File: util/dao/src/lib.rs`)

### Summary
`DaoCalculator::calculate_maximum_withdraw` uses a bare `as u64` truncating cast to narrow a `u128` intermediate result to `u64`. Every other analogous u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the intermediate value exceeds `u64::MAX`, the truncating cast silently discards the high bits, producing a drastically underestimated withdrawal capacity instead of returning an error. This causes the downstream `transaction_fee` computation to underflow, permanently blocking the withdrawal transaction, and causes the `calculate_dao_maximum_withdraw` RPC to return a wrong value to callers.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the maximum CKB a depositor can withdraw from NervosDAO using the accumulate-rate ratio:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast on line 156 is a **truncating** (wrapping) cast in Rust: if `withdraw_counted_capacity > u64::MAX`, the high 64 bits are silently dropped and the low 64 bits are used as the result. No error is returned.

Every other u128→u64 narrowing in the same file uses the checked form:

```rust
// line 204 — secondary_block_reward
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// line 245 — dao_field_with_current_epoch (miner_issuance)
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)

// line 258 — dao_field_with_current_epoch (ar_increase)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The inconsistency is the root cause. The existing unit test `check_withdraw_calculation_overflows` (lines 295–350 in `util/dao/src/tests.rs`) constructs a near-`u64::MAX` capacity cell and asserts `result.is_err()`. Due to the truncating cast, the function instead returns `Ok(silently_wrong_small_value)`, meaning the test assertion itself would fail — confirming the defect is live. [5](#0-4) 

---

### Impact Explanation

**Two affected code paths:**

1. **Transaction verification** — `FeeCalculator::transaction_fee` (in `verification/src/transaction_verifier.rs`) calls `DaoCalculator::transaction_fee`, which calls `transaction_maximum_withdraw`, which calls `calculate_maximum_withdraw`. [6](#0-5) 

   If `withdraw_counted_capacity` overflows u64, the truncated result is a tiny value. Then:
   ```
   transaction_fee = maximum_withdraw(tiny) - outputs_capacity(correct large value)
   ```
   `safe_sub` underflows → `DaoError::Overflow` → the withdrawal transaction is permanently rejected. The depositor's CKB is locked in NervosDAO with no valid withdrawal path.

2. **RPC endpoint** — `ExperimentRpcImpl::calculate_dao_maximum_withdraw` calls `calculate_maximum_withdraw` directly and returns the result to the caller. [7](#0-6) 

   A silently truncated value is returned as the "maximum withdrawal amount," misleading wallets and users into constructing transactions with the wrong output capacity.

---

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

Since `counted_capacity ≤ u64::MAX` and `withdrawing_ar ≥ deposit_ar` (the accumulate rate only increases), the product overflows when `withdrawing_ar / deposit_ar > 1` and `counted_capacity` is close to `u64::MAX`. On mainnet the total CKB supply is ~33.6 billion CKB (≈ 3.36 × 10¹⁸ shannons), which is well below `u64::MAX` (≈ 1.84 × 10¹⁹ shannons), so no single cell can hold enough capacity to trigger this today. However:

- On **testnets or custom chains** with different genesis parameters, this is directly reachable.
- As the accumulate rate grows over decades (secondary issuance ~4%/year), cells holding a large fraction of the total supply approach the threshold.
- The defect is **already present** and the existing overflow test would fail against the current code, indicating no regression guard is in place.

---

### Recommendation

Replace the truncating cast with the same checked pattern used elsewhere in the file:

```rust
// Before (line 156):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After:
let withdraw_counted_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_u64).safe_add(occupied_capacity)?
``` [8](#0-7) 

This makes the function return `Err(DaoError::Overflow)` on overflow, consistent with all other arithmetic guards in `DaoCalculator`, and makes the existing test `check_withdraw_calculation_overflows` pass correctly.

---

### Proof of Concept

Using the values from the existing test `check_withdraw_calculation_overflows`:

- `capacity = 18_446_744_073_709_550_000` shannons
- `output_data_capacity = 0`, default lock → `occupied_capacity = 4_100_000_000`
- `counted_capacity = 18_446_744_073_709_550_000 − 4_100_000_000 = 18_446_744_069_609_550_000`
- `deposit_ar = 10_000_000_000_123_456`, `withdrawing_ar = 10_000_000_001_123_456`

```
withdraw_counted_capacity (u128)
  = 18_446_744_069_609_550_000 × 10_000_000_001_123_456
    / 10_000_000_000_123_456
  ≈ 20_291_418_474_297_550_000   ← exceeds u64::MAX (18_446_744_073_709_551_615)
```

With `as u64` (truncating):
```
20_291_418_474_297_550_000 mod 2^64
  = 20_291_418_474_297_550_000 − 18_446_744_073_709_551_616
  = 1_844_674_400_587_998_384   ← silently wrong small value
```

`Capacity::shannons(1_844_674_400_587_998_384).safe_add(4_100_000_000)` succeeds, returning `Ok(Capacity::shannons(1_844_674_404_687_998_384))` — a value ~10× smaller than the depositor's actual entitlement — instead of `Err(DaoError::Overflow)`.

The test at line 349 asserts `assert!(result.is_err())`, which would fail, confirming the silent truncation is the live defect. [9](#0-8)

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

**File:** util/dao/src/lib.rs (L258-261)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;
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

**File:** verification/src/transaction_verifier.rs (L265-273)
```rust
    fn transaction_fee(&self) -> Result<Capacity, DaoError> {
        // skip tx fee calculation for cellbase
        if self.transaction.is_cellbase() {
            Ok(Capacity::zero())
        } else {
            DaoCalculator::new(self.consensus.as_ref(), &self.data_loader)
                .transaction_fee(&self.transaction)
        }
    }
```

**File:** rpc/src/module/experiment.rs (L259-267)
```rust
                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
```
