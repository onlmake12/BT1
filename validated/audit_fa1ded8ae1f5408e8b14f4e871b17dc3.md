### Title
Silent `u64` Truncation in `calculate_maximum_withdraw` Returns Wrong DAO Withdrawal Capacity — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum DAO withdrawal amount as a `u128` intermediate value and then casts it to `u64` with a bare `as u64`. When the intermediate value exceeds `u64::MAX`, the cast silently truncates it to a drastically smaller number, causing the function to return a wrong (much smaller) withdrawal capacity. This affects both the `calculate_dao_maximum_withdraw` RPC endpoint and the consensus-level `transaction_fee` calculation that feeds block assembly and tx-pool admission.

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

The `as u64` cast is a **truncating** cast: if `withdraw_counted_capacity` exceeds `u64::MAX`, the high bits are silently discarded and the function returns a capacity that is `withdraw_counted_capacity mod 2^64` shannons — a value that can be billions of times smaller than the correct answer.

Compare with `secondary_block_reward` in the same file, which performs the identical pattern but uses the safe conversion:

```rust
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) 

The inconsistency is unambiguous: one path propagates overflow as an error; the other silently corrupts the result.

The same function is called from two production paths:

1. **RPC** — `ExperimentRpcImpl::calculate_dao_maximum_withdraw` (both `WithdrawingHeaderHash` and `WithdrawingOutPoint` branches) calls `calculator.calculate_maximum_withdraw(...)` and returns the result directly to the caller. [3](#0-2) [4](#0-3) 

2. **Consensus / tx-pool** — `DaoCalculator::transaction_fee` calls `transaction_maximum_withdraw`, which calls `calculate_maximum_withdraw` for every DAO-type input in a resolved transaction. [5](#0-4) [6](#0-5) 

The existing regression test `check_withdraw_calculation_overflows` constructs a cell with capacity `18_446_744_073_709_550_000` shannons (≈ u64::MAX − 1615) and asserts `result.is_err()`. With the current `as u64` cast, the intermediate u128 value overflows u64::MAX, truncates to `≈ 1_840_574_448_384`, and `safe_add(occupied_capacity)` succeeds — so the function returns `Ok(small_capacity)` instead of `Err(Overflow)`, meaning the test assertion fails and the overflow goes undetected. [7](#0-6) 

---

### Impact Explanation

**Impact: High**

When `withdraw_counted_capacity` overflows `u64`, the returned capacity is a tiny fraction of the correct value. A user who deposits a very large amount of CKB into the NervosDAO and later calls `calculate_dao_maximum_withdraw` to learn how much they can withdraw receives a drastically underestimated figure. If they construct their phase-2 withdrawal transaction using this figure, they claim far less than they are entitled to. The unclaimed interest (and potentially principal) remains permanently locked in the DAO cell, because the phase-2 transaction has already consumed the phase-1 cell.

Additionally, `transaction_fee` (used by the tx-pool and block assembler) will compute a wrong fee for any DAO withdrawal transaction whose maximum-withdraw overflows, potentially causing incorrect fee ordering or rejection of valid high-value withdrawals.

---

### Likelihood Explanation

**Likelihood: Low–Medium**

The overflow condition requires:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

Since `withdrawing_ar ≥ deposit_ar` always holds (the accumulation rate only increases), any deposit whose `counted_capacity` is close to `u64::MAX` will overflow as soon as the AR has grown at all. The total CKB supply is ≈ 33.6 billion CKB = 3.36 × 10¹⁸ shannons; `u64::MAX` ≈ 18.4 × 10¹⁸ shannons. A single entity depositing ≈ 18 billion CKB (plausible for a large custodian or exchange) and holding for years would trigger the bug. The AR grows slowly, so the truncation error starts small but grows over time.

---

### Recommendation

Replace the bare cast with the checked conversion already used elsewhere in the same codebase:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64)

// After (consistent with secondary_block_reward):
Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
)
``` [8](#0-7) 

---

### Proof of Concept

**Trigger path (RPC caller, no privileges required):**

1. Deposit ≈ 18.4 billion CKB (close to `u64::MAX` shannons) into the NervosDAO via a phase-1 deposit transaction.
2. Mine enough blocks for the accumulation rate (`AR`) to increase by any nonzero amount.
3. Submit a phase-1 prepare transaction.
4. Call the RPC:
   ```json
   {
     "method": "calculate_dao_maximum_withdraw",
     "params": [
       { "tx_hash": "<deposit_tx_hash>", "index": "0x0" },
       "<withdrawing_block_hash>"
     ]
   }
   ```
5. The RPC internally computes:
   ```
   withdraw_counted_capacity  =  counted_capacity × withdrawing_ar / deposit_ar
                               ≈  u64::MAX + δ   (overflows u64)
   ```
   The `as u64` cast truncates to `δ` (a small value). The RPC returns `δ + occupied_capacity` shannons — a tiny fraction of the correct withdrawal amount.
6. A user who constructs their phase-2 withdrawal transaction using this figure receives far less CKB than they deposited plus interest. The remainder is permanently locked.

**Root cause line:** [8](#0-7) 

**Inconsistent safe path (same file, same pattern, correct implementation):** [2](#0-1) 

**Affected RPC implementation:** [9](#0-8)

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

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
```

**File:** util/dao/src/lib.rs (L295-350)
```rust
                        .and_then(|c| tx_capacities.safe_add(c))
                })
                .and_then(|c| capacities.safe_add(c))
        })?;

        Ok(added_occupied_capacities)
    }

    fn input_occupied_capacities(&self, rtx: &ResolvedTransaction) -> CapacityResult<Capacity> {
        rtx.resolved_inputs
            .iter()
            .try_fold(Capacity::zero(), |capacities, cell_meta| {
                let current_capacity = modified_occupied_capacity(cell_meta, self.consensus);
                current_capacity.and_then(|c| capacities.safe_add(c))
            })
    }

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
}

/// return special occupied capacity if cell is satoshi's gift
/// otherwise return cell occupied capacity
pub fn modified_occupied_capacity(
    cell_meta: &CellMeta,
    consensus: &Consensus,
) -> CapacityResult<Capacity> {
    if let Some(tx_info) = &cell_meta.transaction_info
        && tx_info.is_genesis()
        && tx_info.is_cellbase()
        && cell_meta.cell_output.lock().args().raw_data() == consensus.satoshi_pubkey_hash.0[..]
    {
        return Into::<Capacity>::into(cell_meta.cell_output.capacity())
            .safe_mul_ratio(consensus.satoshi_cell_occupied_ratio);
    }
    cell_meta.occupied_capacity()
```

**File:** rpc/src/module/experiment.rs (L235-299)
```rust
    fn calculate_dao_maximum_withdraw(
        &self,
        out_point: OutPoint,
        kind: DaoWithdrawingCalculationKind,
    ) -> Result<Capacity> {
        let snapshot: &Snapshot = &self.shared.snapshot();
        let consensus = snapshot.consensus();
        let out_point: packed::OutPoint = out_point.into();
        let data_loader = snapshot.borrow_as_data_loader();
        let calculator = DaoCalculator::new(consensus, &data_loader);
        match kind {
            DaoWithdrawingCalculationKind::WithdrawingHeaderHash(withdrawing_header_hash) => {
                let (tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output = tx
                    .outputs()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output_data = tx
                    .outputs_data()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
            }
            DaoWithdrawingCalculationKind::WithdrawingOutPoint(withdrawing_out_point) => {
                let (_tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                let withdrawing_out_point: packed::OutPoint = withdrawing_out_point.into();
                let (withdrawing_tx, withdrawing_header_hash) = snapshot
                    .get_transaction(&withdrawing_out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;

                let output = withdrawing_tx
                    .outputs()
                    .get(withdrawing_out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;
                let output_data = withdrawing_tx
                    .outputs_data()
                    .get(withdrawing_out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash,
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
            }
        }
    }
```
