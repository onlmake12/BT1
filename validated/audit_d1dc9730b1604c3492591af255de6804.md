### Title
Silent `u128 → u64` Truncation in DAO Withdrawal Capacity Calculation Produces Incorrect Withdrawal Amount - (File: `util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum withdrawable capacity using a `u128` intermediate, then silently truncates it to `u64` via an `as u64` cast. Every other analogous calculation in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The silent truncation produces an incorrect (too-small) withdrawal amount when the intermediate exceeds `u64::MAX`, causing legitimate DAO withdrawal transactions to be rejected from the tx-pool and the `calculate_dao_maximum_withdraw` RPC to return a wrong value.

### Finding Description

In `calculate_maximum_withdraw` (`util/dao/src/lib.rs`, lines 152–156):

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `withdraw_counted_capacity as u64` cast silently truncates the u128 result modulo `2^64`. If the true value exceeds `u64::MAX`, the truncated value is arbitrarily small, and the function returns a capacity far below the correct withdrawal amount.

By contrast, every other analogous division in the same file uses the checked conversion:

```rust
// dao_field_with_current_epoch, line 244-245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// dao_field_with_current_epoch, line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;

// secondary_block_reward, line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

The inconsistency is the root cause. `calculate_maximum_withdraw` is the only site that uses the unsafe `as u64` cast.

### Impact Explanation

When `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`, the truncated value is less than the correct withdrawal amount. This flows into `transaction_fee`:

```rust
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))
        .map_err(Into::into)
}
``` [4](#0-3) 

If the truncated `maximum_withdraw` is less than `outputs_capacity`, `safe_sub` returns an error and the transaction is rejected from the tx-pool — a denial-of-service against the DAO depositor. If the truncated value is still above `outputs_capacity`, the fee is underestimated, causing incorrect prioritization.

Additionally, the `calculate_dao_maximum_withdraw` RPC calls this function directly and returns the wrong value to users: [5](#0-4) 

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity × withdrawing_ar / deposit_ar > u64::MAX ≈ 1.84 × 10¹⁹
```

With `deposit_ar = 10^16` (genesis) and `counted_capacity` near the total CKB supply (~3.36 × 10¹⁸ shannons), `withdrawing_ar` must exceed ~5.5 × 10¹⁶ — a factor of ~5.5 growth from genesis. Given the slow accumulate-rate growth, this requires many decades of chain operation. However, a depositor who locked CKB at genesis and withdraws far in the future is a realistic long-term scenario. The bug is also immediately observable via the RPC for any caller who crafts a header with a sufficiently large `ar` value in a test or devnet environment.

### Recommendation

Replace the silent cast with the checked conversion used everywhere else in the file:

```diff
-let withdraw_capacity =
-    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
+let withdraw_counted_capacity_u64 =
+    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
+let withdraw_capacity =
+    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

### Proof of Concept

1. Construct a deposit header with `ar = 10_000_000_000_000_000` (genesis default).
2. Construct a withdrawing header with `ar = 55_000_000_000_000_000` (5.5× growth).
3. Create a DAO cell with `capacity = u64::MAX - occupied_capacity` (maximum possible counted capacity).
4. Call `DaoCalculator::calculate_maximum_withdraw` with these headers.
5. Observe: `counted_capacity × 55_000_000_000_000_000 / 10_000_000_000_000_000 = counted_capacity × 5.5`, which exceeds `u64::MAX`.
6. The `as u64` cast silently wraps the result to a small value.
7. The returned capacity is far below the correct withdrawal amount.
8. `transaction_fee` returns `Err` (underflow in `safe_sub`), causing the withdrawal transaction to be rejected from the tx-pool. [6](#0-5) [7](#0-6)

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

**File:** util/dao/src/lib.rs (L126-159)
```rust
    /// Calculate maximum withdraw capacity of a deposited dao output
    pub fn calculate_maximum_withdraw(
        &self,
        output: &CellOutput,
        output_data_capacity: Capacity,
        deposit_header_hash: &Byte32,
        withdrawing_header_hash: &Byte32,
    ) -> Result<Capacity, DaoError> {
        let deposit_header = self
            .data_loader
            .get_header(deposit_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        let withdrawing_header = self
            .data_loader
            .get_header(withdrawing_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        if deposit_header.number() >= withdrawing_header.number() {
            return Err(DaoError::InvalidOutPoint);
        }

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
    }
```

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
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

**File:** util/dao/utils/src/lib.rs (L16-17)
```rust
// This is multiplied by 10**16 to make sure we have enough precision.
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
```
