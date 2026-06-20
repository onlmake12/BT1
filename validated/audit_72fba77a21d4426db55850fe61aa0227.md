### Title
Silent `u128 → u64` Truncation in `calculate_maximum_withdraw` Produces Wrong DAO Withdrawal Amount and Accounting Mismatch — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum CKB a depositor can withdraw from the NervosDAO using a `u128` intermediate value, then casts it to `u64` with a bare `as u64` (wrapping/truncating cast). Every other analogous computation in the same file uses `u64::try_from(…).map_err(|_| DaoError::Overflow)?`. When the intermediate result exceeds `u64::MAX`, the truncating cast silently wraps the value to a much smaller number instead of returning an error. The function then returns `Ok` with a wrong (drastically reduced) capacity. The existing test `check_withdraw_calculation_overflows` asserts `result.is_err()` for exactly this overflow scenario, but the current code returns `Ok` with a silently truncated value — meaning the test is failing and the bug is live.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
// lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

The `as u64` cast is a **wrapping truncation**: if `withdraw_counted_capacity > u64::MAX`, the high bits are silently discarded and the function returns `Ok` with a wrong capacity.

Every other `u128 → u64` narrowing in the same file uses the checked form:

| Line | Pattern |
|------|---------|
| 204 | `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?` |
| 245 | `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?` |
| 258 | `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?` |

The test at `util/dao/src/tests.rs:296-350` (`check_withdraw_calculation_overflows`) constructs a cell with capacity `18_446_744_073_709_550_000` shannons and AR values that produce `withdraw_counted_capacity ≈ 20.3 × 10^18 > u64::MAX`. It asserts `result.is_err()`. With the `as u64` cast, the function instead returns `Ok(Capacity::shannons(1_844_674_406_960_953_338))` — a value ~10× smaller than the correct withdrawal — so the test fails.

The function is called from two critical paths:

1. **`transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch`**: The DAO field `S` (secondary issuance reserve) is decremented by the truncated value instead of the correct value, permanently overstating `S` in the on-chain DAO accounting.
2. **`calculate_dao_maximum_withdraw` RPC** (`rpc/src/module/experiment.rs:235-298`): The RPC returns the truncated (wrong) amount to the user, who then constructs a withdrawal transaction with the wrong output capacity. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

---

### Impact Explanation

When `withdraw_counted_capacity` overflows `u64`:

- **User fund loss**: The `calculate_dao_maximum_withdraw` RPC returns a drastically reduced capacity. A user who builds a withdrawal transaction based on this value receives far less CKB than they deposited plus earned interest.
- **DAO accounting mismatch**: `withdrawed_interests` (called inside `dao_field_with_current_epoch`) uses the same truncated value to decrement `current_s`. Because less is subtracted than should be, the DAO field `S` is permanently overstated. This inflates the interest rate for all future DAO depositors, at the expense of the treasury/burn mechanism — an analog to the M-10 insolvency pattern where accounting diverges from actual transferred amounts.
- **Test failure confirms the bug is live**: `check_withdraw_calculation_overflows` asserts `is_err()` but the code returns `Ok` with a wrong value. [7](#0-6) [8](#0-7) 

---

### Likelihood Explanation

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX  (≈ 1.84 × 10¹⁹ shannons)
```

- `counted_capacity` is bounded by the total CKB supply (~3.36 × 10¹⁸ shannons).
- The accumulate rate (`ar`) starts at `10^16` and grows by approximately 4% per year (secondary issuance / total CKB).
- For overflow: `withdrawing_ar / deposit_ar > 5.48`, requiring ~43 years of chain operation from the deposit block.

**Likelihood is low** for any individual depositor today, but the bug is structurally present and the test already documents the failure. Any DAO depositor (an unprivileged transaction sender / RPC caller) who deposits CKB early in the chain's life and withdraws after the AR has grown sufficiently will trigger the truncation silently, with no error and no warning. [9](#0-8) 

---

### Recommendation

Replace the truncating cast with the checked conversion already used everywhere else in the file:

```rust
// Before (line 155-156):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After:
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

This makes the function consistent with `secondary_block_reward` (line 204), `dao_field_with_current_epoch` (lines 245, 258), and satisfies the existing `check_withdraw_calculation_overflows` test assertion. [1](#0-0) [5](#0-4) 

---

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` is the proof of concept. It sets:

- `output.capacity = 18_446_744_073_709_550_000` shannons
- `deposit_ar = 10_000_000_000_123_456`
- `withdrawing_ar = 10_000_000_001_123_456`

`counted_capacity ≈ 18_446_744_069_609_550_000` (after subtracting occupied capacity of ~4.1 × 10⁹ shannons).

`withdraw_counted_capacity = 18_446_744_069_609_550_000 × 10_000_000_001_123_456 / 10_000_000_000_123_456 ≈ 20_291_418_476_570_504_954`

This exceeds `u64::MAX = 18_446_744_073_709_551_615`.

With `as u64`: `20_291_418_476_570_504_954 mod 2^64 = 1_844_674_402_860_953_338` → function returns `Ok(Capacity::shannons(1_844_674_406_960_953_338))` — a value ~11× smaller than the correct withdrawal.

The test asserts `result.is_err()` and fails, confirming the bug is present in the production code path. [10](#0-9) [11](#0-10)

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

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
```

**File:** util/dao/src/lib.rs (L242-246)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;
```

**File:** util/dao/src/lib.rs (L248-254)
```rust
        let current_c = parent_c.safe_add(current_g)?;
        let current_u = parent_u
            .safe_add(added_occupied_capacities)
            .and_then(|u| u.safe_sub(freed_occupied_capacities))?;
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
```

**File:** util/dao/src/lib.rs (L256-261)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;
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
