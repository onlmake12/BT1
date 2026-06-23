### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Produces Wrong DAO Withdrawal Capacity — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes a u128 intermediate result and then casts it to u64 with a silent truncating `as u64`. Every other analogous u128→u64 narrowing in the same file uses the checked `u64::try_from(…).map_err(|_| DaoError::Overflow)?` pattern. When the u128 intermediate value exceeds `u64::MAX`, the silent cast wraps the value to a small number, the subsequent `safe_add` succeeds with a completely wrong (far-too-small) capacity, and the function returns `Ok(wrong_value)` instead of `Err(DaoError::Overflow)`. This corrupts both the per-transaction fee accounting and the on-chain DAO field (`withdrawed_interests`), which accumulates into every subsequent block's DAO state.

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

The `ar` (accumulate rate) is a dimensionless ratio scaled by `10^16` (`DEFAULT_GENESIS_ACCUMULATE_RATE = 10_000_000_000_000_000`). [2](#0-1) 

Because `withdrawing_ar ≥ deposit_ar` always holds (ar is monotonically non-decreasing), `withdraw_counted_capacity ≥ counted_capacity`. When `counted_capacity` is large and `withdrawing_ar` has grown sufficiently relative to `deposit_ar`, the u128 product exceeds `u64::MAX`. The Rust `as u64` cast silently takes the low 64 bits, producing a value that can be arbitrarily small (e.g., near zero), and the subsequent `safe_add(occupied_capacity)` succeeds without error.

**Contrast with every other u128→u64 narrowing in the same file:**

```rust
// secondary_block_reward — line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch — line 244-245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// dao_field_with_current_epoch — line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [3](#0-2) [4](#0-3) 

All three use `u64::try_from`. Only `calculate_maximum_withdraw` uses the unsafe `as u64` cast.

---

### Impact Explanation

`calculate_maximum_withdraw` feeds two critical paths:

1. **`transaction_fee`** — computes `maximum_withdraw - outputs_capacity`. If `maximum_withdraw` is silently truncated to a small value, a withdrawal transaction whose outputs match the truncated amount is accepted with a near-zero fee, bypassing the intended fee enforcement.

2. **`withdrawed_interests`** (called from `dao_field_with_current_epoch`) — the wrong value is subtracted from the running DAO secondary-issuance accumulator `s`. This corrupts the on-chain DAO field packed into every subsequent block header, permanently skewing the `ar` growth rate and all future DAO interest calculations for every depositor. [5](#0-4) [6](#0-5) 

---

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`counted_capacity` is bounded by the total CKB supply (~3.36 × 10¹⁸ shannons), while `u64::MAX ≈ 18.4 × 10¹⁸`. This means `withdrawing_ar / deposit_ar` must exceed ~5.5×, i.e., `ar` must grow to more than 5.5 times its genesis value. Given the slow secondary issuance rate, this would take an extremely long time on mainnet. However:

- The condition is **reachable in principle** by any unprivileged transaction sender who submits a DAO withdrawal transaction.
- On a chain with a modified genesis (e.g., a testnet or a chain spec with a high `secondary_epoch_reward`), the threshold is reached much sooner.
- The code inconsistency itself (silent `as u64` vs. checked `try_from` everywhere else) is a latent defect that will eventually be reachable as the chain ages.

---

### Recommendation

Replace the silent cast with the same checked pattern used everywhere else in the file:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

This makes the overflow explicit and consistent with `secondary_block_reward`, `miner_issuance`, and `ar_increase` in the same file. [7](#0-6) 

---

### Proof of Concept

**Trigger condition** (arithmetic):

```
deposit_ar  = 10_000_000_000_000_000          // genesis ar (10^16)
withdrawing_ar = 55_000_000_000_000_000       // ar grown 5.5×
counted_capacity = 3_360_000_000_000_000_000  // ~33.6 billion CKB in shannons (total supply)

withdraw_counted_capacity (u128)
  = 3_360_000_000_000_000_000 × 55_000_000_000_000_000
    / 10_000_000_000_000_000
  = 18_480_000_000_000_000_000
  > u64::MAX (18_446_744_073_709_551_615)

as u64 → 18_480_000_000_000_000_000 mod 2^64
        = 18_480_000_000_000_000_000 - 18_446_744_073_709_551_616
        = 33_255_926_290_448_384   ← completely wrong, ~33× too small
```

A DAO withdrawal transaction spending a cell with `counted_capacity = 3.36 × 10¹⁸` shannons on a chain where `ar` has grown to 5.5× genesis would have its maximum-withdraw silently computed as ~33 quadrillion shannons instead of ~18.48 × 10¹⁸ shannons. The transaction would be accepted with a wrong fee, and the DAO field `s` in every subsequent block header would be corrupted by the difference. [8](#0-7) [2](#0-1)

### Citations

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

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
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

**File:** util/dao/utils/src/lib.rs (L16-17)
```rust
// This is multiplied by 10**16 to make sure we have enough precision.
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
```
