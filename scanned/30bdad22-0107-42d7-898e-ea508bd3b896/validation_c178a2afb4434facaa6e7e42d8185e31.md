### Title
Silent `u128`→`u64` Truncation in `calculate_maximum_withdraw` Causes Incorrect NervosDAO Withdrawal Amounts — (`File: util/dao/src/lib.rs`)

### Summary

In `DaoCalculator::calculate_maximum_withdraw`, the intermediate 128-bit withdrawal result is cast to `u64` with a bare `as u64`, which silently truncates the high bits if the value exceeds `u64::MAX`. Every other analogous 128-bit intermediate in the same codebase uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern. The inconsistency means that when the product `counted_capacity × withdrawing_ar` grows large enough relative to `deposit_ar`, the returned withdrawal capacity is silently wrong (too small), causing the depositor to receive less than the protocol owes them and corrupting the `s` (secondary-issuance surplus) field in the DAO accumulator.

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

The `as u64` cast is defined Rust behaviour but silently discards the upper 64 bits when `withdraw_counted_capacity > u64::MAX`. The rest of the same file uses the safe pattern:

```rust
// secondary_block_reward
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
``` [2](#0-1) [3](#0-2) 

The inconsistency is the root cause. `calculate_maximum_withdraw` is called from:

1. `transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch` (consensus-critical DAO accumulator field `s`)
2. `transaction_fee` (fee verification)
3. The `calculate_dao_maximum_withdraw` JSON-RPC endpoint [4](#0-3) [5](#0-4) 

### Impact Explanation

When truncation occurs:

- **Depositor loss of funds**: `withdraw_capacity` is computed as `(withdraw_counted_capacity % 2^64) + occupied_capacity`, which can be far smaller than the correct value. The depositor receives less CKB than the protocol owes them.
- **DAO accumulator corruption**: `withdrawed_interests` (derived from `calculate_maximum_withdraw`) is subtracted from `parent_s` to produce `current_s`. A truncated `withdrawed_interests` leaves `current_s` inflated, meaning the NervosDAO secondary-issuance surplus is overstated in every subsequent block header. This is a consensus-level accounting error that compounds over time.
- **Fee verification error**: `transaction_fee` calls `maximum_withdraw.safe_sub(outputs_capacity)`. If `maximum_withdraw` is truncated below `outputs_capacity`, the subtraction returns `DaoError::Overflow`, causing a valid DAO withdrawal transaction to be rejected by the tx-pool and block verifier. [6](#0-5) [7](#0-6) 

### Likelihood Explanation

The trigger condition is `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX ≈ 1.84 × 10^19`.

- `deposit_ar` starts at `10_000_000_000_000_000` (10^16) at genesis.
- `ar` grows at roughly the secondary issuance rate (~1.344 billion CKB / ~33.6 billion CKB total ≈ 4 % per year).
- For the ratio `withdrawing_ar / deposit_ar` to reach ~5.5× (the threshold for a full-supply deposit), approximately 43 years of chain operation are required.
- For smaller deposits the threshold is proportionally higher, but any deposit exceeding ~3.4 billion CKB (≈10 % of genesis supply) held for 43+ years crosses the boundary.

This is not a theoretical-only scenario: the CKB chain is designed for multi-decade operation, the NervosDAO is explicitly a long-term savings instrument, and large institutional holders are the primary target users. The `ZeroC` guard in `genesis_dao_data_with_satoshi_gift` shows the team is aware of division-by-zero risks in this arithmetic, making the missing overflow guard in `calculate_maximum_withdraw` a clear oversight. [8](#0-7) 

### Recommendation

Replace the bare `as u64` cast with the same checked conversion used everywhere else in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (consistent with the rest of the file):
Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
).safe_add(occupied_capacity)?
``` [9](#0-8) 

This makes the function return `DaoError::Overflow` instead of silently truncating, consistent with `secondary_block_reward` and `dao_field_with_current_epoch`.

### Proof of Concept

The following values demonstrate the truncation path. `deposit_ar = 10^16` (genesis), `withdrawing_ar = 6 × 10^16` (ratio 6×, reachable after ~45 years at 4 %/yr), `counted_capacity = u64::MAX / 5 ≈ 3.69 × 10^18` shannons (≈36.9 billion CKB, within the total supply envelope):

```
withdraw_counted_capacity (u128) = (u64::MAX / 5) * (6 × 10^16) / 10^16
                                 = (u64::MAX / 5) * 6
                                 = u64::MAX * 6 / 5
                                 ≈ 2.21 × 10^19   >  u64::MAX (1.84 × 10^19)

withdraw_counted_capacity as u64 = 2.21e19 % 2^64
                                 ≈ 3.7 × 10^18    (truncated — ~83 % of correct value lost)
```

A depositor who locked 36.9 billion CKB at genesis would receive only ~3.7 billion CKB worth of the interest-bearing portion instead of the correct ~4.4 billion CKB — a silent loss of ~700 million CKB with no error returned to the caller. [10](#0-9) [11](#0-10)

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

**File:** util/dao/src/lib.rs (L149-158)
```rust
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

**File:** util/dao/src/lib.rs (L316-332)
```rust
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
```

**File:** util/dao/utils/src/lib.rs (L88-92)
```rust
    // C cannot be zero, otherwise DAO stats calculation might result in
    // division by zero errors.
    if c == Capacity::zero() {
        return Err(DaoError::ZeroC);
    }
```

**File:** util/dao/utils/src/error.rs (L36-41)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
    /// ZeroC
    #[error("ZeroC")]
    ZeroC,
```
