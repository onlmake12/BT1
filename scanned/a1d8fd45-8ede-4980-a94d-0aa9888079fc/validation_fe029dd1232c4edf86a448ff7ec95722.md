### Title
Silent Truncating Cast on DAO Withdrawal Capacity Bypasses Overflow Check - (`util/dao/src/lib.rs`)

### Summary

`calculate_maximum_withdraw` in `util/dao/src/lib.rs` casts a `u128` intermediate result to `u64` using the silent truncating `as u64` operator instead of the checked `u64::try_from(...)` used by every other analogous computation in the same file. If the intermediate value exceeds `u64::MAX`, the high bits are silently discarded, producing an incorrect (smaller) maximum-withdrawal capacity that propagates into DAO field computation and block validation.

### Finding Description

`calculate_maximum_withdraw` computes the maximum amount a NervosDAO depositor may withdraw:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

Every other `u128 → u64` narrowing in the same file uses a **checked** conversion that returns `DaoError::Overflow` on overflow:

| Site | Conversion used |
|---|---|
| `miner_issuance128` (line 244) | `u64::try_from(...).map_err(|_| DaoError::Overflow)?` |
| `ar_increase128` (line 258) | `u64::try_from(...).map_err(|_| DaoError::Overflow)?` |
| `reward128` (line 204) | `u64::try_from(...).map_err(|_| DaoError::Overflow)?` |
| **`withdraw_counted_capacity` (line 156)** | **`as u64` — silent truncation** | [2](#0-1) [3](#0-2) [4](#0-3) 

The formula is `counted_capacity × withdrawing_ar / deposit_ar`. Because `withdrawing_ar ≥ deposit_ar` (the accumulation rate only ever increases), the result is always `≥ counted_capacity`. When the ratio `withdrawing_ar / deposit_ar` grows large enough that the product exceeds `u64::MAX ≈ 1.84 × 10¹⁹`, the cast wraps to a much smaller value.

### Impact Explanation

`calculate_maximum_withdraw` feeds two critical paths:

1. **Block DAO-field validation** — `DaoHeaderVerifier::verify` calls `dao_field` → `dao_field_with_current_epoch` → `withdrawed_interests` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw`. A truncated `withdraw_counted_capacity` makes `withdrawed_interests` smaller than the true value, so `current_s` (NervosDAO savings) in the computed DAO field is inflated. Because every full node runs the same code, all nodes accept the inflated DAO field, permanently corrupting the on-chain DAO accounting state. [5](#0-4) 

2. **`calculate_dao_maximum_withdraw` RPC** — the RPC endpoint calls `calculate_maximum_withdraw` directly and returns the truncated (under-reported) capacity to users, causing DAO depositors to believe they are entitled to less than they actually are. [6](#0-5) 

### Likelihood Explanation

The overflow condition requires `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. With the total CKB supply capped at ≈ 3.36 × 10¹⁸ shannons and the initial AR at 10¹⁶, the ratio `withdrawing_ar / deposit_ar` must exceed ≈ 5.5. At current secondary-issuance rates this takes many decades, making the issue latent rather than immediately exploitable. However, the inconsistency is a clear defect: the identical pattern is handled safely everywhere else in the same file, and the missing check is the only thing preventing a future consensus-corrupting overflow.

### Recommendation

Replace the silent cast with the same checked conversion used by all other analogous sites in the file:

```rust
let withdraw_capacity = Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
).safe_add(occupied_capacity)?;
``` [7](#0-6) 

### Proof of Concept

Construct a `calculate_maximum_withdraw` call where:
- `counted_capacity = u64::MAX` (maximum possible cell capacity)
- `withdrawing_ar = 2 × deposit_ar` (AR has doubled since deposit)

Then `withdraw_counted_capacity = u64::MAX × 2 = 2^65 - 2`, which exceeds `u64::MAX`. The `as u64` cast produces `u64::MAX - 1` (wraps), a value that is `u64::MAX` less than the correct result. The returned `withdraw_capacity` is therefore nearly `u64::MAX` shannons smaller than the depositor is entitled to, and the DAO field written into the block header carries an inflated `current_s` that all nodes accept as valid.

### Citations

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

**File:** util/dao/src/lib.rs (L242-245)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
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
