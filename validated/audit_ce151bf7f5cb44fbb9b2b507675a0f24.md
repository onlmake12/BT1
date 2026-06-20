### Title
Silent u128→u64 Truncation in DAO Withdrawal Calculation Produces Wrong Withdraw Amount — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum CKB a depositor can withdraw from the NervosDAO using a `u128` intermediate value, then casts it to `u64` with a bare `as u64` (silent truncation). Every other identical `u128→u64` conversion in the same file uses the checked `u64::try_from(…).map_err(|_| DaoError::Overflow)?` pattern. When the intermediate product overflows `u64::MAX`, the cast silently wraps to a tiny value, causing the user to receive far less than their entitled withdrawal. The existing regression test `check_withdraw_calculation_overflows` is itself broken by this bug: it asserts `result.is_err()`, but the code returns `Ok(wrong_small_value)` instead of an error.

---

### Finding Description

In `calculate_maximum_withdraw` the withdrawal amount is computed as:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast is a **wrapping truncation**: if `withdraw_counted_capacity > u64::MAX`, the lower 64 bits are silently kept and the upper bits are discarded. The result is an arbitrarily small (potentially near-zero) capacity value.

Every other `u128→u64` narrowing in the same file is guarded:

```rust
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
// line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)
// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The regression test `check_withdraw_calculation_overflows` constructs exactly the overflow scenario (capacity near `u64::MAX`, `withdrawing_ar` slightly larger than `deposit_ar`) and asserts `result.is_err()`:

```rust
// util/dao/src/tests.rs  lines 296-349
assert!(result.is_err());
``` [5](#0-4) 

With the `as u64` cast the function returns `Ok(Capacity::shannons(~1_844_672_789))` — a value billions of times smaller than the correct withdrawal — so the test assertion itself panics, confirming the bug is live.

The `DaoHeaderVerifier` calls `dao_field_with_current_epoch`, which in turn calls `transaction_maximum_withdraw` → `calculate_maximum_withdraw` when processing blocks containing DAO withdrawal transactions:

```rust
// verification/contextual/src/contextual_block_verifier.rs  lines 300-318
pub fn verify(&self) -> Result<(), Error> {
    let dao = DaoCalculator::new(...)
        .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
        ...?;
    if dao != self.header.dao() {
        return Err((BlockErrorKind::InvalidDAO).into());
    }
    Ok(())
}
``` [6](#0-5) 

---

### Impact Explanation

A NervosDAO depositor who holds a cell with capacity close to `u64::MAX` shannons and withdraws after the accumulate-rate (`ar`) has grown sufficiently will have `withdraw_counted_capacity` overflow `u64`. The `as u64` truncation wraps the result to a tiny value. The user's `withdraw_capacity` is computed as that tiny value plus `occupied_capacity`, which is accepted as valid by the verifier (no error is raised). The depositor loses the bulk of their principal and all accrued interest with no on-chain indication of error.

Additionally, the broken test means the overflow path is not caught by CI, so the defect can persist undetected.

---

### Likelihood Explanation

The overflow requires `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. Because the total CKB supply is ~3.36 × 10¹⁸ shannons and `ar` grows at roughly 4 % per year, a single cell would need `ar` to grow by a factor of ~5.5× from deposit to withdrawal — approximately 43 years at current issuance rates. The scenario is therefore not exploitable in the near term on mainnet. However:

- The test `check_withdraw_calculation_overflows` already constructs the overflow case and **expects an error that is never returned**, meaning the defect is demonstrably present and the safety net is absent.
- Any future change to issuance parameters or a very long-lived deposit could bring the threshold closer.

---

### Recommendation

Replace the bare `as u64` cast with the same checked conversion used everywhere else in the file:

```rust
// util/dao/src/lib.rs  line 155-156
let withdraw_capacity = Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
).safe_add(occupied_capacity)?;
```

This makes the function return `Err(DaoError::Overflow)` on overflow (consistent with all sibling calculations) and restores the correctness of the existing test.

---

### Proof of Concept

The existing test at `util/dao/src/tests.rs:296–349` already encodes the proof of concept:

- `output.capacity = 18_446_744_073_709_550_000` (≈ `u64::MAX`)
- `deposit_ar = 10_000_000_000_123_456`, `withdrawing_ar = 10_000_000_001_123_456`
- `withdraw_counted_capacity ≈ 18_446_744_075_554_224_405 > u64::MAX`
- With `as u64`: result is `Ok(Capacity::shannons(~1_844_672_789 + occupied_capacity))` — a silently wrong tiny value
- Test asserts `result.is_err()` → **test panics**, confirming the bug [7](#0-6)

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

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-319)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao = DaoCalculator::new(
            &self.context.consensus,
            &self.context.store.borrow_as_data_loader(),
        )
        .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
        .map_err(|e| {
            error_target!(
                crate::LOG_TARGET,
                "Error generating dao data for block {}: {:?}",
                self.header.hash(),
                e
            );
            e
        })?;

        if dao != self.header.dao() {
            return Err((BlockErrorKind::InvalidDAO).into());
        }
        Ok(())
```
