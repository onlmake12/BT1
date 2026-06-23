### Title
Silent u128→u64 Truncation in DAO Withdrawal Capacity Calculation — (`File: util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes `withdraw_counted_capacity` in `u128` to avoid intermediate overflow, but then casts the result back to `u64` with a bare `as u64`, silently truncating any bits above bit 63. The sibling function `secondary_block_reward` in the same file performs an identical u128 intermediate computation but correctly uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The inconsistency is a direct analog to the report's "incorrect maximum value representation" class: a value that should be range-checked is instead silently wrapped, producing a mathematically wrong result that propagates into consensus-enforced capacity accounting.

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

The same file's `secondary_block_reward` performs the same pattern but uses the safe path:

```rust
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) 

`withdraw_counted_capacity = counted_capacity × withdrawing_ar / deposit_ar`. Because `withdrawing_ar ≥ deposit_ar` (interest always accrues), the result is always ≥ `counted_capacity`. If the ratio `withdrawing_ar / deposit_ar` grows large enough that the product exceeds `u64::MAX ≈ 1.84 × 10¹⁹`, the `as u64` cast wraps the value modulo 2⁶⁴, producing a drastically smaller (and wrong) capacity figure. No error is returned; the corrupted value is passed directly into `Capacity::shannons` and then into `safe_add`.

The result of `calculate_maximum_withdraw` feeds `transaction_maximum_withdraw`, which feeds `transaction_fee`:

```rust
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))
        .map_err(Into::into)
}
``` [3](#0-2) 

A truncated `maximum_withdraw` that is smaller than `outputs_capacity` causes `safe_sub` to return `Err(Overflow)`, which propagates as a transaction validation failure.

### Impact Explanation

Any DAO withdrawal transaction whose `withdraw_counted_capacity` exceeds `u64::MAX` will be permanently rejected by every honest node running this code path. The user's deposited CKB becomes unwithdrawable through the normal protocol path. Because the same `DaoCalculator` is used by all nodes, the rejection is consensus-wide: no honest miner can include the transaction, and no honest node will accept a block that does. The effect is a permanent, protocol-level denial-of-service on affected DAO cells — the deposited capacity is locked forever.

### Likelihood Explanation

The accumulation rate `ar` starts at `ar₀ = 10¹⁶` (confirmed in test code). Secondary issuance grows `ar` at roughly 4 % per year relative to total locked capacity. For `withdraw_counted_capacity` to overflow `u64::MAX ≈ 1.84 × 10¹⁹` given a maximum realistic `counted_capacity` of ~3.36 × 10¹⁸ shannons (total CKB supply), the ratio `withdrawing_ar / deposit_ar` must exceed ~5.5. At 4 % annual compounding that takes on the order of 43 years. The likelihood is therefore negligible in the near term but non-zero over the full lifetime of the chain. The risk is elevated for cells deposited very early (low `deposit_ar`) and withdrawn very late (high `withdrawing_ar`).

### Recommendation

Replace the bare cast with the same checked conversion already used in `secondary_block_reward`:

```rust
// Before
Capacity::shannons(withdraw_counted_capacity as u64)

// After
let withdraw_counted_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_u64)
```

This makes overflow an explicit, recoverable error consistent with the rest of the DAO calculator.

### Proof of Concept

Construct a DAO deposit cell with `counted_capacity = C` shannons. Wait (or simulate) until `ar` has grown such that `C × withdrawing_ar / deposit_ar > u64::MAX`. Submit a withdrawal transaction. Every node will compute `withdraw_counted_capacity` in u128, silently truncate it to a value near zero via `as u64`, compute `withdraw_capacity ≈ occupied_capacity`, and then fail `safe_sub(outputs_capacity)` because `outputs_capacity > occupied_capacity`. The transaction is rejected on every node with `DaoError` (capacity overflow), making the deposit permanently unwithdrawable through the standard protocol path.

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
