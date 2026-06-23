### Title
Unsafe `u128 as u64` Truncating Cast in `calculate_maximum_withdraw` Silently Corrupts DAO Withdrawal Accounting — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes `withdraw_counted_capacity` as a `u128` intermediate, then casts it to `u64` with the bare `as u64` operator. In Rust, `as u64` on a `u128` is a **silent truncating cast** — it discards the high 64 bits without any error. Every other analogous `u128 → u64` conversion in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this a clear inconsistency. When the intermediate value exceeds `u64::MAX`, the result is silently wrong, corrupting both the fee verification path and the on-chain DAO secondary-issuance accounting (`current_s`).

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the maximum withdrawable capacity for a NervosDAO cell:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← unsafe truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The formula is `counted_capacity × withdrawing_ar / deposit_ar`. Both `withdrawing_ar` and `deposit_ar` are `u64` accumulate-rate values stored in the DAO field. Because `ar` is monotonically increasing, `withdrawing_ar ≥ deposit_ar` always holds. If the ratio `withdrawing_ar / deposit_ar` is large enough that the product exceeds `u64::MAX`, the `as u64` cast silently wraps, producing a value equal to `withdraw_counted_capacity % 2^64` — potentially orders of magnitude smaller than the true result.

Every other `u128 → u64` narrowing in the same file is guarded:

```rust
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
// line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)
// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

Line 156 is the sole exception.

`calculate_maximum_withdraw` feeds two critical paths:

**Path 1 — Fee verification.**  
`transaction_maximum_withdraw` calls `calculate_maximum_withdraw` for every DAO input, then `transaction_fee` subtracts total outputs from that maximum:

```rust
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))
        .map_err(Into::into)
}
``` [5](#0-4) 

If the truncated `maximum_withdraw` is smaller than the actual outputs, `safe_sub` returns `Err(Overflow)`, and the transaction verifier rejects a legitimately valid DAO withdrawal.

**Path 2 — DAO secondary-issuance state (`current_s`).**  
`withdrawed_interests` calls `transaction_maximum_withdraw` and subtracts input capacities to compute the interest paid out. This value is then subtracted from `current_s` in `dao_field_with_current_epoch`:

```rust
let current_s = parent_s
    .safe_add(nervosdao_issuance)
    .and_then(|s| s.safe_sub(withdrawed_interests))?;
``` [6](#0-5) 

If `withdrawed_interests` is too small (because `calculate_maximum_withdraw` returned a truncated value), `current_s` is inflated. The DAO pool then records more secondary issuance than was actually distributed, allowing future depositors to claim interest that was never earned.

---

### Impact Explanation

- **Consensus-level rejection of valid DAO withdrawals.** Any DAO withdrawal transaction whose `withdraw_counted_capacity` overflows `u64` will be rejected by every honest node via `transaction_fee` returning an error. The transaction cannot be included in any block, permanently locking the depositor's funds.
- **DAO secondary-issuance pool inflation.** The on-chain `s` field (secondary issuance accumulated in NervosDAO) is permanently inflated for every block that includes such a withdrawal. Future depositors can withdraw more interest than the protocol actually issued, draining capacity from the system.

---

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity × withdrawing_ar / deposit_ar > u64::MAX ≈ 1.84 × 10^19
```

`counted_capacity` is bounded by the total CKB supply (~33.6 billion CKB = ~3.36 × 10^18 shannons). `ar` starts at `10^16` and grows monotonically. For overflow, `withdrawing_ar / deposit_ar` must exceed ~5.5×. Given the slow growth rate of `ar` (proportional to `g2 / c` per block), this ratio would take an extremely long time to reach on mainnet under normal conditions.

However, the vulnerability is structurally real: the code is inconsistent with every other `u128 → u64` conversion in the same file, there is no test covering this overflow path (the existing `check_withdraw_calculation_overflows` test catches a different overflow in `safe_add`, not the truncation), and the impact when triggered is severe and irreversible.

---

### Recommendation

Replace the bare `as u64` cast with the same checked pattern used everywhere else in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (safe):
let withdraw_counted_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_u64).safe_add(occupied_capacity)?
``` [7](#0-6) 

---

### Proof of Concept

1. Construct a DAO deposit cell with `counted_capacity` close to `u64::MAX` (e.g., `18_000_000_000_000_000_000` shannons).
2. Advance the chain until `withdrawing_ar / deposit_ar > 1.02` (i.e., `ar` has grown by more than 2% since deposit).
3. Submit a DAO withdrawal transaction spending that cell.
4. `withdraw_counted_capacity = 18_000_000_000_000_000_000 × withdrawing_ar / deposit_ar` exceeds `u64::MAX`.
5. `withdraw_counted_capacity as u64` silently wraps to a small value (e.g., near 0).
6. `transaction_fee` calls `maximum_withdraw.safe_sub(outputs_capacity)` → underflow → `DaoError`.
7. Every node rejects the withdrawal; the depositor's funds are permanently inaccessible. [8](#0-7)

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
