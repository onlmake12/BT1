### Title
Silent `u128 → u64` Truncation in `calculate_maximum_withdraw` Corrupts NervosDAO Savings Field in Consensus-Critical DAO Field Computation — (File: `util/dao/src/lib.rs`)

---

### Summary

`calculate_maximum_withdraw` in `util/dao/src/lib.rs` uses a silent `as u64` cast to narrow a `u128` intermediate result. Every other analogous intermediate in the same file uses `u64::try_from(…).map_err(|_| DaoError::Overflow)?`. If the intermediate value exceeds `u64::MAX`, the `as u64` cast silently wraps (truncates to the lower 64 bits), producing a wrong — potentially near-zero — withdrawal amount. This wrong value propagates through `withdrawed_interests` → `dao_field_with_current_epoch` into the consensus-critical DAO field `S` (NervosDAO savings), permanently overstating it. Because `DaoHeaderVerifier` recomputes the DAO field using the same code path, all nodes accept the corrupted field, making the corruption consensus-final.

---

### Finding Description

In `calculate_maximum_withdraw`:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

Compare with the two other `u128 → u64` narrowings in the same file, both of which use checked conversion:

```rust
// line 244-245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

The `calculate_maximum_withdraw` result is consumed by `transaction_maximum_withdraw`, which feeds `withdrawed_interests`:

```rust
// line 330-332
maximum_withdraws
    .safe_sub(input_capacities)
    .map_err(Into::into)
``` [4](#0-3) 

`withdrawed_interests` is then subtracted from `current_s` inside `dao_field_with_current_epoch`:

```rust
// line 252-254
let current_s = parent_s
    .safe_add(nervosdao_issuance)
    .and_then(|s| s.safe_sub(withdrawed_interests))?;
``` [5](#0-4) 

If `withdraw_counted_capacity` overflows `u64`, the truncated (smaller) value makes `withdrawed_interests` smaller than it should be, so `current_s` is **overstated**.

The consensus verifier `DaoHeaderVerifier::verify` recomputes the DAO field using the exact same `dao_field()` call and compares it to the block header:

```rust
// verification/contextual/src/contextual_block_verifier.rs  lines 300-318
let dao = DaoCalculator::new(…)
    .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)…?;
if dao != self.header.dao() {
    return Err((BlockErrorKind::InvalidDAO).into());
}
``` [6](#0-5) 

Because every node runs the same truncating code, all nodes compute the same wrong `S` value, accept the block, and the corruption becomes consensus-final. The `ZeroC` guard in `genesis_dao_data_with_satoshi_gift` (with the comment *"C cannot be zero, otherwise DAO stats calculation might result in division by zero errors"*) shows the developers were aware of precision risks in this subsystem, yet the analogous guard for the `as u64` narrowing was never added. [7](#0-6) 

---

### Impact Explanation

| Surface | Effect |
|---|---|
| **Consensus / DAO field `S`** | `S` is permanently overstated in every block that contains a DAO withdrawal whose `withdraw_counted_capacity` overflows. Future depositors earn more interest than the protocol intends — an uncontrolled inflation of NervosDAO yield. |
| **Tx-pool fee calculation** | `transaction_fee` = `maximum_withdraw − outputs_capacity`. A truncated `maximum_withdraw` can underflow, causing the node to reject a valid DAO withdrawal transaction entirely (denial-of-service for the depositor). |
| **RPC `calculate_dao_maximum_withdraw`** | Returns a wrong (much smaller) value, misleading wallets and users about the actual withdrawable amount. |

---

### Likelihood Explanation

The overflow condition is `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. Because `withdrawing_ar / deposit_ar` grows very slowly (the accumulation rate starts at `10^16` and increases by roughly `~10^5` per block), reaching a ratio large enough to overflow `u64` for any realistic deposit requires an extremely long time horizon or a deposit approaching the entire CKB supply. The likelihood is therefore **low** under current mainnet conditions. However, the defect is a real, demonstrable code inconsistency — every other `u128 → u64` narrowing in the same file uses `try_from` — and the impact when triggered is consensus-final corruption of the NervosDAO savings field.

---

### Recommendation

Replace the silent cast with a checked conversion, consistent with the rest of the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64)

// After (safe, consistent with lines 244-245 and 258):
Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
)
```

Additionally, add an invariant assertion (or return `DaoError`) that `current_s` does not decrease unexpectedly after processing withdrawals, analogous to the report's recommendation to assert the non-decreasing invariant.

---

### Proof of Concept

1. Deposit an amount of CKB close to `u64::MAX` shannons into the NervosDAO.
2. Wait until `withdrawing_ar` has grown sufficiently that `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX` (e.g., after many epochs of secondary issuance accumulation).
3. Submit a DAO withdrawal (phase 2) transaction. The node calls `calculate_maximum_withdraw`.
4. `withdraw_counted_capacity as u64` wraps around, producing a value far smaller than the correct amount (potentially near zero).
5. `withdrawed_interests` is computed with this truncated value.
6. `current_s = parent_s + nervosdao_issuance − (truncated withdrawed_interests)` is overstated.
7. The block producer embeds this wrong `S` in the DAO field. `DaoHeaderVerifier` recomputes the same wrong value and accepts the block.
8. All subsequent DAO depositors earn inflated interest because `S` is permanently overstated.
9. Simultaneously, the depositor's own withdrawal transaction may be rejected by the tx-pool because `transaction_fee = truncated_maximum_withdraw − outputs_capacity` underflows.

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
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

**File:** util/dao/src/lib.rs (L256-258)
```rust
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

**File:** util/dao/utils/src/lib.rs (L88-92)
```rust
    // C cannot be zero, otherwise DAO stats calculation might result in
    // division by zero errors.
    if c == Capacity::zero() {
        return Err(DaoError::ZeroC);
    }
```
