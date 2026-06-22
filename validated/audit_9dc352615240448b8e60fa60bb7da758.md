### Title
Silent u128→u64 Truncation in DAO Maximum Withdrawal Calculation — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes an intermediate `u128` withdrawal amount and then silently truncates it to `u64` via `as u64`. Every other analogous u128→u64 conversion in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this a clear inconsistency. When the u128 value exceeds `u64::MAX`, the cast wraps silently, causing the function to return a drastically incorrect (much smaller) capacity instead of propagating an overflow error.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The expression `withdraw_counted_capacity` is a `u128`. The cast `as u64` is a **silent modular truncation**: if the value exceeds `u64::MAX` (2⁶⁴ − 1), it wraps to `withdraw_counted_capacity % 2⁶⁴` with no error, no panic, and no signal to the caller.

Every other u128→u64 narrowing in the same file uses the checked form:

```rust
// Line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
// Line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)
// Line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) 

The `calculate_maximum_withdraw` function is the sole exception.

The existing overflow test (`check_withdraw_calculation_overflows`) does **not** cover the silent truncation path — it relies on the downstream `safe_add` to catch overflow after the cast, which only works when the truncated value plus `occupied_capacity` still exceeds `u64::MAX`. When the truncated value is small enough that `safe_add` succeeds, the function silently returns a wrong `Ok(small_capacity)`. [3](#0-2) 

---

### Impact Explanation

`calculate_maximum_withdraw` is called in two production paths:

**1. Transaction fee verification (`transaction_maximum_withdraw` → `transaction_fee`):** [4](#0-3) [5](#0-4) 

When the truncated `withdraw_counted_capacity` wraps to a small value and `safe_add(occupied_capacity)` succeeds, `transaction_fee` computes `maximum_withdraw.safe_sub(outputs_capacity)`. Since `maximum_withdraw` is now a tiny incorrect value while `outputs_capacity` is the legitimate large withdrawal amount, `safe_sub` underflows and returns a capacity error. The DAO withdrawal transaction is **rejected by the node** even though it is fully valid. This is a consensus-level DoS on DAO withdrawals.

**2. `calculate_dao_maximum_withdraw` RPC:** [6](#0-5) 

The RPC returns a silently wrong (much smaller) capacity value to the caller, misleading wallets and users about their entitled withdrawal amount.

---

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity × withdrawing_ar / deposit_ar > u64::MAX
```

`ar` (accumulation rate) starts at `10_000_000_000_000_000` (10¹⁶) and grows proportionally to secondary issuance. For the ratio `withdrawing_ar / deposit_ar` to cause overflow on a cell holding the maximum plausible capacity (~3.36 × 10¹⁸ shannons, the total CKB supply), the ratio must exceed ~5.5×. At current secondary issuance rates this would take an extremely long time. However:

- The condition is **not attacker-controlled** — it is a function of elapsed time and DAO participation.
- The code defect is **real and present today**, and the inconsistency with all other conversions in the same file is unambiguous evidence of a bug rather than an intentional design choice.
- Any future parameter change (e.g., higher secondary issuance in a testnet or devnet) could make this reachable much sooner.

---

### Recommendation

Replace the silent `as u64` cast with the same checked pattern used everywhere else in the file:

```rust
// Before (unsafe):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After (safe, consistent with rest of file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [7](#0-6) 

---

### Proof of Concept

The following values demonstrate the silent truncation path (distinct from the existing test which relies on `safe_add` to catch the error):

- `deposit_ar` = `10_000_000_000_000_000`
- `withdrawing_ar` = `60_000_000_000_000_000` (6× growth — far-future scenario)
- `counted_capacity` = `3_000_000_000_000_000_000` shannons (3 × 10¹⁸, within total supply)

```
withdraw_counted_capacity (u128) = 3e18 × 6e16 / 1e16 = 18e18
```

`18_000_000_000_000_000_000 > u64::MAX (18_446_744_073_709_551_615)` — **false** in this example, but:

- `counted_capacity` = `3_500_000_000_000_000_000`, `withdrawing_ar/deposit_ar` = 6×:
  `withdraw_counted_capacity = 21_000_000_000_000_000_000 > u64::MAX`
  `as u64` → `21_000_000_000_000_000_000 % 2^64 = 2_553_255_926_290_448_384` (a small, wrong value)
  `safe_add(occupied_capacity)` succeeds → function returns `Ok(wrong_small_capacity)` silently.

The existing test `check_withdraw_calculation_overflows` does **not** catch this case because it relies on `safe_add` overflowing after the cast, not on the cast itself. [1](#0-0) [8](#0-7)

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

**File:** util/dao/src/lib.rs (L108-113)
```rust
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
```

**File:** util/dao/src/lib.rs (L152-158)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

        Ok(withdraw_capacity)
```

**File:** util/dao/src/lib.rs (L242-258)
```rust
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
