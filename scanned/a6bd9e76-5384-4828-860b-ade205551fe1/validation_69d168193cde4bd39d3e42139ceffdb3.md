### Title
Unsafe `u128 as u64` Truncating Cast in NervosDAO Withdrawal Capacity Calculation Produces Silently Distorted Withdrawal Amounts — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` in `util/dao/src/lib.rs` performs an unsafe `as u64` truncating cast on a `u128` intermediate result. If the intermediate value exceeds `u64::MAX`, the cast silently wraps, producing a drastically incorrect (much smaller) maximum-withdraw capacity. Every other `u128→u64` narrowing in the same file uses the safe `u64::try_from(…).map_err(|_| DaoError::Overflow)?` pattern; this one site is the sole exception.

---

### Finding Description

In `calculate_maximum_withdraw`:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← unsafe truncating cast
        .safe_add(occupied_capacity)?;
```

`withdraw_counted_capacity` is a `u128`. The `as u64` cast silently discards the upper 64 bits when the value exceeds `u64::MAX = 18_446_744_073_709_551_615`. The resulting `Capacity` is then used as the authoritative maximum-withdraw figure for the DAO cell.

Every other `u128→u64` narrowing in the same file is guarded:

```rust
// secondary_block_reward  (line 204)
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch  (lines 244-245, 258)
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

The inconsistency is structural: the same author-intent (overflow → `DaoError::Overflow`) is expressed safely everywhere except `calculate_maximum_withdraw`.

---

### Impact Explanation

`calculate_maximum_withdraw` feeds directly into `transaction_maximum_withdraw` → `transaction_fee` (the consensus-level fee check for DAO-withdraw transactions). A silently truncated result has two concrete effects:

1. **Distorted fee computation**: `transaction_fee = maximum_withdraw − outputs_capacity`. If `maximum_withdraw` is truncated to a value smaller than `outputs_capacity`, `safe_sub` returns `CapacityError::Overflow`, and the transaction is **rejected by the node** even though it is protocol-valid. Legitimate DAO withdrawals become permanently unspendable.

2. **Incorrect capacity accounting**: Any caller that uses the returned `Capacity` to decide how much CKB a depositor may claim receives a wildly wrong figure, enabling under-payment or spurious rejection.

---

### Likelihood Explanation

The overflow condition is `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`.

- `counted_capacity` is bounded by the total CKB supply (≈ 3.36 × 10¹⁸ shannons initially, growing with secondary issuance toward `u64::MAX` over ~112 years).
- `withdrawing_ar / deposit_ar` grows at roughly 4 % per year (secondary issuance ≈ 1.344 × 10⁹ CKB/year over a ~33.6 × 10⁹ CKB base).
- Overflow requires the ratio to exceed ≈ 5.5×, which takes on the order of 100+ years of continuous chain operation.

Likelihood is therefore **very low** in the near term but **non-zero** over the full intended lifetime of the chain. No attacker action is required; the condition arises purely from the passage of time and normal secondary issuance. The vulnerability is not attacker-controlled but is reachable by any ordinary DAO depositor submitting a withdrawal transaction after the threshold is crossed.

---

### Recommendation

Replace the unsafe cast with the same checked pattern used everywhere else in the file:

```rust
// Before (unsafe)
Capacity::shannons(withdraw_counted_capacity as u64)

// After (safe, consistent with the rest of the file)
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64)
```

---

### Proof of Concept

The unsafe cast site: [1](#0-0) 

Safe counterparts in the same file that demonstrate the intended pattern: [2](#0-1) [3](#0-2) [4](#0-3) 

The `DaoError::Overflow` variant that should be returned on overflow: [5](#0-4) 

The downstream fee-check path that consumes the distorted value: [6](#0-5)

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

**File:** util/dao/src/lib.rs (L204-204)
```rust
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
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

**File:** util/dao/utils/src/error.rs (L36-38)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
```
