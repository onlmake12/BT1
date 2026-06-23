### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Produces Incorrect DAO Withdrawal Capacity — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the DAO withdrawal capacity using a u128 intermediate value and then converts it to u64 via a silent `as u64` cast. This truncates the upper 64 bits without any overflow check, producing a silently wrong result. Every other analogous u128→u64 conversion in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this inconsistency the root cause of the bug.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The cast `withdraw_counted_capacity as u64` silently truncates the lower 64 bits of the u128 result. If `withdraw_counted_capacity` exceeds `u64::MAX`, the result wraps to `withdraw_counted_capacity % 2^64`, which can be arbitrarily smaller than the correct value — and no error is returned.

Compare this to every other u128→u64 narrowing in the same file:

- `secondary_block_reward` (line 204): `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` (line 245): `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` (line 258): `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?` [2](#0-1) [3](#0-2) 

All three use the safe checked conversion. Only `calculate_maximum_withdraw` uses the silent `as u64` cast.

The overflow condition: `counted_capacity * withdrawing_ar > u64::MAX * deposit_ar`. Since `ar` starts at `10_000_000_000_000_000` (10^16) and `counted_capacity` can be up to ~`1.8 × 10^19` (u64::MAX in shannons), the intermediate product `counted_capacity * withdrawing_ar` can reach ~`1.8 × 10^35`, which fits in u128 but the post-division result can exceed u64::MAX when the ar ratio is sufficiently large relative to a near-maximum capacity cell.

---

### Impact Explanation

`calculate_maximum_withdraw` is called in two paths:

1. **RPC path** — `calculate_dao_maximum_withdraw` in `rpc/src/module/experiment.rs` (line 259) calls it directly and returns the result to any RPC caller. [4](#0-3) 

2. **Consensus-critical path** — `transaction_maximum_withdraw` (line 108) calls it during `transaction_fee` computation, which is used to validate that a DAO withdrawal transaction does not claim more than it is entitled to. [5](#0-4) 

When overflow occurs and the truncated value is smaller than the correct value, `transaction_fee` computes `maximum_withdraw.safe_sub(outputs_capacity)` where `maximum_withdraw` is artificially deflated. This causes a valid DAO withdrawal transaction to be rejected as having negative fee — a permanent DoS against the depositor's withdrawal. The depositor's funds become unrecoverable through the normal withdrawal path.

---

### Likelihood Explanation

The overflow requires a large `counted_capacity` (near u64::MAX in shannons, i.e., ~184 billion CKB) and a meaningful ar ratio growth. While this is a high capacity threshold, the CKB protocol imposes no cap on individual cell capacity, and the ar ratio grows continuously over the chain's lifetime. As the chain matures, the ar ratio grows, lowering the capacity threshold required to trigger the overflow. The condition is reachable by any DAO depositor who deposits a sufficiently large cell — no privileged access is required.

---

### Recommendation

Replace the silent cast with the checked conversion already used consistently elsewhere in the same file:

```rust
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

This matches the pattern at lines 204, 245, and 258 and ensures that overflow is surfaced as a `DaoError::Overflow` rather than silently producing a wrong capacity value.

---

### Proof of Concept

**Entry point**: RPC caller or transaction sender submitting a DAO withdrawal.

**Trigger condition** (arithmetic):
- `deposit_ar` = 10_000_000_000_000_000 (initial ar)
- `withdrawing_ar` = 10_000_000_001_000_000 (after modest growth)
- `counted_capacity` = 18_446_744_073_000_000_000 (near u64::MAX)

Intermediate: `18_446_744_073_000_000_000 * 10_000_000_001_000_000 = ~1.844 × 10^35`

After dividing by `deposit_ar` (10^16): `~1.844 × 10^19 > u64::MAX (1.844 × 10^19)`

The `as u64` cast silently wraps, producing a value far below the correct withdrawal amount. The `transaction_fee` call then computes `maximum_withdraw.safe_sub(outputs_capacity)` where `outputs_capacity` (the correct withdrawal amount) exceeds the truncated `maximum_withdraw`, causing the transaction to be rejected with a capacity error — permanently blocking the depositor's withdrawal. [6](#0-5)

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

**File:** util/dao/src/lib.rs (L256-259)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
```

**File:** rpc/src/module/experiment.rs (L259-266)
```rust
                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
```
