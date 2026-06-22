### Title
Precision Loss in `DaoCalculator::dao_field_with_current_epoch()` Causes Permanent Underissuance of DAO Interest — (`util/dao/src/lib.rs`)

### Summary
The accumulate-rate increment (`ar_increase`) computed every block in `dao_field_with_current_epoch()` uses integer-division truncation. Because `ar` is the sole basis for computing each NervosDAO depositor's interest in `calculate_maximum_withdraw()`, the per-block truncation error accumulates permanently. The total secondary issuance credited to NervosDAO (`S` field) always exceeds what depositors can collectively claim, and the difference is permanently unclaimable.

### Finding Description
In `util/dao/src/lib.rs`, `dao_field_with_current_epoch()` updates the DAO accumulate rate each block:

```rust
let ar_increase128 =
    u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
let current_ar = parent_ar
    .checked_add(ar_increase)
    .ok_or(DaoError::Overflow)?;
``` [1](#0-0) 

The integer division `parent_ar × g2 / C` truncates, silently discarding up to 1 unit of `ar` per block. This error is never corrected or carried forward.

The `S` field is incremented by `nervosdao_issuance = g2 − floor(g2 × U / C)` each block:

```rust
let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;
...
let current_s = parent_s
    .safe_add(nervosdao_issuance)
    .and_then(|s| s.safe_sub(withdrawed_interests))?;
``` [2](#0-1) 

When a depositor withdraws, `calculate_maximum_withdraw()` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [3](#0-2) 

Because `withdrawing_ar` is slightly lower than the exact value (due to accumulated per-block truncation), the depositor receives slightly less interest than entitled. A second truncation in this final

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L246-254)
```rust
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;

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
