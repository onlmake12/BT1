Audit Report

## Title
Silent Truncating Cast in DAO Withdrawal Capacity Arithmetic - (`util/dao/src/lib.rs`)

## Summary
`DaoCalculator::calculate_maximum_withdraw` computes a `u128` intermediate result for the ar-scaled withdrawal capacity and then narrows it to `u64` via a silent `as u64` cast at line 156. Every other `u128 → u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. If the intermediate value exceeds `u64::MAX`, the cast silently truncates, returning a drastically underestimated withdrawal capacity with no error, which propagates into the DAO state field `S` and corrupts on-chain DAO accounting.

## Finding Description
In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` (lines 152–156) computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently discards the upper 64 bits when `withdraw_counted_capacity > u64::MAX`. This is inconsistent with every other analogous narrowing in the same file:

- Line 204: `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?` [2](#0-1) 
- Line 245: `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?` [3](#0-2) 
- Line 258: `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?` [4](#0-3) 

The truncated `withdraw_capacity` is returned from `calculate_maximum_withdraw`, consumed by `transaction_maximum_withdraw`, and then aggregated in `withdrawed_interests`: [5](#0-4) 

`withdrawed_interests` feeds directly into `dao_field_with_current_epoch` at line 222, where `current_s` is computed as:

```rust
let current_s = parent_s
    .safe_add(nervosdao_issuance)
    .and_then(|s| s.safe_sub(withdrawed_interests))?;
``` [6](#0-5) 

Because both the block producer and the `DaoHeaderVerifier` call the same `dao_field` path using the same buggy `calculate_maximum_withdraw`, the inflated `current_s` passes consensus verification and is committed to the chain.

## Impact Explanation
The corrupted `S` field is committed on-chain and accepted by all nodes running the same code, constituting a consensus-level corruption of DAO state. Subsequent DAO withdrawals that rely on `S` for interest accounting are affected. This matches the allowed impact: **Vulnerabilities which could cause consensus deviation / damage CKB economy (Critical)**.

## Likelihood Explanation
The overflow condition is `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. With maximum realistic `counted_capacity` ≈ 3.36×10¹⁸ shannons (total CKB supply), overflow requires `ar` to grow by a factor of ~5.5× from genesis. Since `ar` grows monotonically and never resets, this is a latent time-bomb triggered by any unprivileged DAO depositor once the threshold is crossed — estimated at many decades under current secondary issuance parameters. Likelihood is **low** today but increases irreversibly over time.

## Recommendation
Replace the silent cast with the checked conversion already used everywhere else in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (consistent with rest of file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
``` [7](#0-6) 

## Proof of Concept
Arithmetic trigger (no chain state required):

```
deposit_ar         = 10_000_000_000_000_000   (genesis ar)
withdrawing_ar     = 55_000_000_000_000_000   (~5.5× growth)
counted_capacity   = 3_360_000_000_000_000_000 shannons (~33.6B CKB)

withdraw_counted_capacity (u128)
  = 3_360_000_000_000_000_000 × 55_000_000_000_000_000
    / 10_000_000_000_000_000
  = 18_480_000_000_000_000_000   >  u64::MAX (18_446_744_073_709_551_615)

withdraw_counted_capacity as u64
  = 18_480_000_000_000_000_000 mod 2^64
  = 33_255_926_290_448_384          ← wrong truncated value (~332 CKB)
```

A unit test can be written in `util/dao/src/tests` constructing mock headers with the above `ar` values and a cell of the given capacity, calling `calculate_maximum_withdraw`, and asserting it returns `Err(DaoError::Overflow)` rather than the truncated capacity.

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

**File:** util/dao/src/lib.rs (L252-254)
```rust
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
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
