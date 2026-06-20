### Title
Silent u128→u64 Truncation in DAO Withdrawal Capacity Calculation Produces Wrong Invariant Result — (File: `util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the interest-adjusted withdrawal capacity using a u128 intermediate, then silently truncates it to u64 with an `as u64` cast. Every other analogous arithmetic operation in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The missing checked conversion is a broken invariant: if the u128 result exceeds `u64::MAX`, the truncated value is silently accepted, producing a wrong (too-small) withdrawal amount. This causes the `FeeCalculator` to compute a negative fee and reject an otherwise-valid DAO withdrawal transaction, or causes the `calculate_dao_maximum_withdraw` RPC to return a wrong value to callers.

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

The `as u64` cast silently discards the high 64 bits if `withdraw_counted_capacity > u64::MAX`. No error is returned; the truncated value is used as-is.

Every other u128→u64 narrowing in the same file uses a checked conversion:

```rust
// secondary_block_reward
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) 

```rust
// dao_field_with_current_epoch
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [3](#0-2) 

The inconsistency is the missing invariant: *the withdrawal capacity must fit in u64 or an error must be returned*.

`calculate_maximum_withdraw` is called by `transaction_maximum_withdraw`, which feeds `transaction_fee`:

```rust
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))
        .map_err(Into::into)
}
``` [4](#0-3) 

`transaction_fee` is called inside `ContextualTransactionVerifier::verify`:

```rust
let fee = self.fee_calculator.transaction_fee()?;
``` [5](#0-4) 

If `withdraw_counted_capacity` silently truncates to a value smaller than `outputs_capacity`, `safe_sub` returns an underflow error, and the entire transaction is rejected — even though the DAO type script would accept it.

The same function is exposed directly to RPC callers:

```rust
match calculator.calculate_maximum_withdraw(
    &output,
    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
    &deposit_header_hash,
    &withdrawing_header_hash.into(),
) {
    Ok(capacity) => Ok(capacity.into()),
    ...
}
``` [6](#0-5) 

The `CapacityVerifier` deliberately skips the `OutputsSumOverflow` check for DAO transactions, delegating entirely to the type script:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    // capacity balance check
}
``` [7](#0-6) 

This means the only node-side capacity invariant for DAO withdrawals is the fee calculator's call to `calculate_maximum_withdraw`. A wrong result there has direct protocol consequences.

---

### Impact Explanation

If `withdraw_counted_capacity` overflows u64, two outcomes are possible:

1. **Truncated value + `occupied_capacity` overflows u64**: `safe_add` returns an error, which propagates correctly — but for the wrong reason (overflow instead of the correct overflow check).
2. **Truncated value + `occupied_capacity` fits in u64**: The function returns a silently wrong (too-small) capacity. `transaction_fee` then computes `maximum_withdraw.safe_sub(outputs_capacity)`, which underflows and rejects a valid DAO withdrawal transaction. The RPC also returns a wrong value to any caller using it to construct a withdrawal.

The invariant that must hold — *withdrawal capacity ≥ deposit capacity* — is not enforced at the arithmetic boundary.

---

### Likelihood Explanation

For `withdraw_counted_capacity` to exceed `u64::MAX ≈ 1.84 × 10^19`:

```
counted_capacity × withdrawing_ar / deposit_ar > 1.84 × 10^19
```

With total CKB supply ≈ 3.36 × 10^18 shannons and `ar` starting at 10^16, the ratio `withdrawing_ar / deposit_ar` would need to exceed ~5.5 (i.e., 450% interest growth). Given CKB's secondary issuance schedule, this would require centuries of accumulation. **Practical likelihood is negligible under current economic parameters.**

However, the missing check is a latent invariant gap: the same arithmetic pattern used safely elsewhere in the file is applied unsafely here, and no test covers the silent-truncation path (the existing `check_withdraw_calculation_overflows` test only exercises the `safe_add` overflow at the end, not the `as u64` truncation).

---

### Recommendation

Replace the silent cast with a checked conversion, consistent with the rest of the file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (checked, consistent with secondary_block_reward and dao_field_with_current_epoch):
let withdraw_counted_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_u64).safe_add(occupied_capacity)?
```

Add a property-based or fuzz test that asserts: for all valid `(counted_capacity, withdrawing_ar, deposit_ar)` triples where `withdrawing_ar ≥ deposit_ar`, `calculate_maximum_withdraw` either returns `Ok` with a value ≥ `output_capacity`, or returns `Err(DaoError::Overflow)` — never silently truncates.

---

### Proof of Concept

Construct a `CellOutput` with `capacity = u64::MAX - occupied_capacity + 1` shannons and headers where `withdrawing_ar / deposit_ar = 2`. Then:

```
counted_capacity = (u64::MAX - occupied_capacity + 1) - occupied_capacity
                 ≈ u64::MAX - 2*occupied_capacity + 1

withdraw_counted_capacity (u128) = counted_capacity * 2 / 1
                                 ≈ 2 * u64::MAX  →  overflows u64

withdraw_counted_capacity as u64  →  silently wraps to a small value
```

`Capacity::shannons(small_value).safe_add(occupied_capacity)` succeeds, returning a capacity far below the correct withdrawal amount. `transaction_fee` then calls `maximum_withdraw.safe_sub(outputs_capacity)`, which underflows and rejects the transaction.

The existing test at `util/dao/src/tests.rs:296` (`check_withdraw_calculation_overflows`) does not cover this path — it uses a capacity near `u64::MAX` with a ratio just above 1, so the overflow is caught by `safe_add`, not by the `as u64` truncation. [8](#0-7)

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

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** verification/src/transaction_verifier.rs (L170-171)
```rust
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
```

**File:** verification/src/transaction_verifier.rs (L483-494)
```rust
        if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
            let inputs_sum = self.resolved_transaction.inputs_capacity()?;
            let outputs_sum = self.resolved_transaction.outputs_capacity()?;

            if inputs_sum < outputs_sum {
                return Err((TransactionError::OutputsSumOverflow {
                    inputs_sum,
                    outputs_sum,
                })
                .into());
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
