### Title
Silent Truncating Cast from `u128` to `u64` in DAO Withdrawal Capacity Calculation — (`util/dao/src/lib.rs`)

---

### Summary

In `util/dao/src/lib.rs`, the `calculate_maximum_withdraw` function computes a `u128` intermediate value (`withdraw_counted_capacity`) and then silently truncates it to `u64` via an unchecked `as u64` cast. Every other analogous `u128`→`u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this omission a clear inconsistency. If the intermediate value exceeds `u64::MAX`, the cast silently wraps, returning a corrupted (too-small) withdrawal capacity to the caller.

---

### Finding Description

In `calculate_maximum_withdraw`:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
```

`withdraw_counted_capacity` is a `u128` product of `counted_capacity` (up to `u64::MAX` shannons) and the ratio `withdrawing_ar / deposit_ar`. The `as u64` cast silently discards the upper 64 bits if the value exceeds `u64::MAX`. [1](#0-0) 

By contrast, every other `u128`→`u64` narrowing in the same file is guarded:

```rust
// line 245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

The `DaoError::Overflow` variant exists precisely for this purpose: [4](#0-3) 

---

### Impact Explanation

If `withdraw_counted_capacity` silently wraps, `Capacity::shannons(...)` receives a truncated value. The returned `withdraw_capacity` is then smaller than the user's actual entitlement. Downstream callers (DAO withdrawal script verification, RPC `calculate_dao_maximum_withdraw`) would return or enforce an incorrect, too-small capacity. A user who deposited a very large amount of CKB into NervosDAO could receive less than they are owed, constituting a direct financial loss. Because the error is silent (no panic, no `Err`), neither the node nor the user would receive any indication that the calculation was wrong.

---

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity * withdrawing_ar / deposit_ar > u64::MAX
```

`counted_capacity` is at most the total deposited CKB minus occupied capacity. The total CKB supply is capped at ~33.6 billion CKB = ~3.36 × 10^18 shannons, which is below `u64::MAX` (~1.84 × 10^19). The ratio `withdrawing_ar / deposit_ar` is always ≥ 1 and grows slowly. For the product to exceed `u64::MAX`, the interest multiplier would need to push the result past ~1.84 × 10^19. This is unlikely under normal chain parameters but is not impossible over a very long chain lifetime or with unusual consensus parameters. The inconsistency with the rest of the function (which uses `try_from`) indicates this was an oversight rather than an intentional design choice.

---

### Recommendation

Replace the silent `as u64` cast with a checked conversion, consistent with the rest of the function:

```rust
// util/dao/src/lib.rs
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
-   Capacity::shannons(withdraw_counted_capacity as u64)
+   Capacity::shannons(u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?)
        .safe_add(occupied_capacity)?;
``` [5](#0-4) 

---

### Proof of Concept

The overflow path is reachable by any transaction sender submitting a DAO withdrawal (phase 2) transaction. The entry path is:

1. User submits a DAO withdrawal phase-2 transaction referencing a deposit cell.
2. The node calls `DaoCalculator::calculate_maximum_withdraw` to verify the output capacity.
3. If `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`, the `as u64` cast silently truncates.
4. The returned `withdraw_capacity` is smaller than the true entitlement.
5. The node accepts the transaction with the truncated (incorrect) capacity, or the user's own RPC call returns a wrong value they rely on to construct the transaction.

Concretely, with a deposit of `u64::MAX / 2` shannons and an `ar` ratio of 3 (i.e., `withdrawing_ar = 3 * deposit_ar`):

```
withdraw_counted_capacity = (u64::MAX / 2) * 3 = ~2.76 × 10^19 > u64::MAX
withdraw_counted_capacity as u64 = ~9.22 × 10^18  // silently truncated
```

The user would receive approximately half their entitled withdrawal amount with no error raised. [6](#0-5)

### Citations

**File:** util/dao/src/lib.rs (L126-158)
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

**File:** util/dao/utils/src/error.rs (L36-41)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
    /// ZeroC
    #[error("ZeroC")]
    ZeroC,
```
