### Title
Silent `u128`-to-`u64` Truncating Cast in `calculate_maximum_withdraw` Produces Wrong Withdrawal Capacity - (File: util/dao/src/lib.rs)

### Summary

`DaoCalculator::calculate_maximum_withdraw` in `util/dao/src/lib.rs` uses a bare `as u64` cast to convert a `u128` intermediate result to `u64`. When the intermediate value exceeds `u64::MAX`, the cast silently truncates the high bits, producing a drastically wrong (smaller) withdrawal capacity with no error. This is the direct CKB analog of the reported Solidity `int256`→`uint256` silent-cast bug: both are incorrect numeric type conversions that silently corrupt a financial calculation.

### Finding Description

In `calculate_maximum_withdraw` at line 152–156:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The expression `withdraw_counted_capacity as u64` is a **silent truncating cast**. In Rust, `as` casts between integer types never panic and never return errors — they silently discard the upper bits when the source value exceeds the target type's range. If `withdraw_counted_capacity > u64::MAX`, the result is `withdraw_counted_capacity % 2^64`, which is an arbitrarily wrong small number.

By contrast, every other analogous overflow-sensitive conversion in the same codebase uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern:

```rust
// dao_field_with_current_epoch — correct pattern
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
``` [2](#0-1) 

```rust
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [3](#0-2) 

The unit test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` explicitly constructs a scenario where `withdraw_counted_capacity` overflows `u64` and asserts `result.is_err()`: [4](#0-3) 

With the current `as u64` cast, the function does **not** return an error in that scenario — it returns `Ok(wrong_small_value)`, meaning the test itself exposes the defect.

### Impact Explanation

`calculate_maximum_withdraw` is called from two paths:

1. **Script verification / block processing** — via `transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch`. A wrong (truncated) withdrawal amount causes the DAO field (`S` component) to be computed incorrectly, corrupting the on-chain DAO accounting for every subsequent block. [5](#0-4) 

2. **Transaction fee validation** — via `transaction_fee` → `transaction_maximum_withdraw`. If the truncated `maximum_withdraw` is smaller than the transaction's actual output capacity, `safe_sub` returns an error, causing a valid DAO withdrawal transaction to be permanently rejected. [6](#0-5) 

3. **RPC `calculate_dao_maximum_withdraw`** — returns a silently wrong capacity to callers, misleading wallets and users about how much they can withdraw. [7](#0-6) 

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity × withdrawing_ar / deposit_ar > u64::MAX
```

`counted_capacity` is `output_capacity − occupied_capacity` (both `u64`). `ar` starts at `10_000_000_000_000_000` (10^16) and grows monotonically. The ratio `withdrawing_ar / deposit_ar` is always ≥ 1. For a cell whose capacity is close to `u64::MAX` shannons and that has been deposited for a long time (large `ar` ratio), the product overflows. The total CKB supply (~3.36 × 10^18 shannons) is below `u64::MAX` (~1.84 × 10^19), but individual cell capacity is an unconstrained `u64` field — a cell can be constructed with capacity up to `u64::MAX`. Any transaction sender who deposits a DAO cell with a very large capacity and later attempts withdrawal can trigger this path. The existing unit test already demonstrates the exact triggering values.

### Recommendation

Replace the silent `as u64` cast with the checked conversion already used elsewhere in the same function:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
```

This makes the overflow explicit and propagates `DaoError::Overflow`, consistent with every other overflow guard in `dao_field_with_current_epoch`.

### Proof of Concept

The existing unit test at `util/dao/src/tests.rs:295–350` already demonstrates the bug. With the current `as u64` code:

- `output.capacity = 18_446_744_073_709_550_000` shannons
- `deposit_ar = 10_000_000_000_123_456`, `withdrawing_ar = 10_000_000_001_123_456`
- `counted_capacity ≈ 18_446_744_069_609_550_000`
- `withdraw_counted_capacity ≈ 18_446_744_069_609_550_000 × (10_000_000_001_123_456 / 10_000_000_000_123_456) > u64::MAX`
- `withdraw_counted_capacity as u64` silently wraps to a small value (~14 billion shannons)
- `safe_add(occupied_capacity)` succeeds, returning `Ok(Capacity::shannons(~18_446_742_453))` — a value ~10^9× smaller than correct
- The test asserts `result.is_err()` but receives `Ok(...)`, demonstrating the defect [8](#0-7)

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
