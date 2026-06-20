### Title
Silent u128→u64 Truncating Cast in DAO Maximum Withdrawal Capacity Calculation — (`File: util/dao/src/lib.rs`)

---

### Summary

In `DaoCalculator::calculate_maximum_withdraw`, the intermediate u128 result `withdraw_counted_capacity` is narrowed to u64 via a bare `as u64` truncating cast. If the product `counted_capacity * withdrawing_ar / deposit_ar` exceeds `u64::MAX`, the high bits are silently discarded and an incorrect (too-small) withdrawal capacity is returned without any error. This is structurally identical to the reported `acl::remove_role` bug: an unsafe arithmetic operation is used where a safe, checked one is required, allowing a silent corruption of the computed value.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the interest-bearing withdrawal amount as follows:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← unsafe truncating cast
        .safe_add(occupied_capacity)?;
```

The multiplication is correctly widened to u128 to avoid overflow during the intermediate computation. However, the final narrowing back to u64 uses `as u64`, which is a **silent truncating cast** — it discards any bits above position 63 without signaling an error.

The same file applies the correct pattern for an analogous intermediate u128 value just a few lines away in `dao_field_with_current_epoch`:

```rust
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

The inconsistency is the root cause: `withdraw_counted_capacity as u64` is the unsafe operation; `u64::try_from(...).map_err(|_| DaoError::Overflow)?` is the safe one.

---

### Impact Explanation

`calculate_maximum_withdraw` is called from two production paths:

1. **`transaction_maximum_withdraw` → `transaction_fee`** (called during transaction verification in `verification/src/transaction_verifier.rs`): If the truncated value is smaller than `outputs_capacity`, `safe_sub` returns `Err(Overflow)`, causing a legitimate DAO withdrawal transaction to be permanently rejected — a denial-of-service against the depositor.

2. **`withdrawed_interests` → `dao_field_with_current_epoch`** (called during block assembly and block verification in `util/dao/src/lib.rs`): If the truncated value is accepted (i.e., the truncated result happens to be small enough that `safe_add` does not overflow), `withdrawed_interests` is under-reported. This causes `current_s` (the NervosDAO secondary issuance accumulator) to be inflated in the committed block header DAO field, silently corrupting the on-chain DAO accounting state for all subsequent blocks.

---

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity * withdrawing_ar / deposit_ar > u64::MAX
```

Since `withdrawing_ar >= deposit_ar` always holds (the accumulate rate is monotonically non-decreasing), overflow requires the ratio `withdrawing_ar / deposit_ar` to be large enough to push a near-maximum `counted_capacity` past `u64::MAX`. Given that `ar` starts at `10^16` and grows by roughly `ar * g2 / C` per block (a small fractional increment), this ratio grows very slowly under normal chain operation. The likelihood of natural overflow on mainnet within any foreseeable time horizon is low.

However, the bug is a latent correctness defect: the safe pattern (`u64::try_from`) is already used for the structurally identical `miner_issuance128` value in the same function, making this an unambiguous oversight rather than an intentional design choice. Any future change to issuance parameters or a long-lived chain could bring the condition closer to reachable.

---

### Recommendation

Replace the unsafe truncating cast with the checked conversion already used elsewhere in the same file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (safe, consistent with miner_issuance128 handling):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
```

---

### Proof of Concept

**Entry path**: An unprivileged transaction sender submits a DAO withdrawal transaction (phase 2). The node calls `DaoCalculator::calculate_maximum_withdraw` during transaction verification.

**Trigger condition**: The cell was deposited when `deposit_ar` was small, and the withdrawal occurs when `withdrawing_ar` is large enough that:

```
(output_capacity - occupied_capacity) * withdrawing_ar / deposit_ar > u64::MAX
```

**Concrete arithmetic**: With `counted_capacity` near `u64::MAX` (≈ `1.844 × 10^19` shannons) and `withdrawing_ar / deposit_ar ≈ 1.0001` (a modest 0.01% growth), the product already exceeds `u64::MAX`. The `as u64` cast wraps the result to a near-zero value. `Capacity::shannons(near_zero).safe_add(occupied_capacity)` succeeds without error, returning a drastically incorrect withdrawal capacity. Downstream, `withdrawed_interests` is under-counted, and the DAO field written into the block header carries an inflated `s` value, corrupting the NervosDAO state permanently. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L242-245)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L249-254)
```rust
        let current_u = parent_u
            .safe_add(added_occupied_capacities)
            .and_then(|u| u.safe_sub(freed_occupied_capacities))?;
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
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
