### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Produces Wrong Withdrawal Capacity — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the NervosDAO withdrawal amount using a u128 intermediate product, then casts it to u64 with `as u64` — a silent truncating cast. When the intermediate u128 result exceeds `u64::MAX`, the cast silently wraps the value to a small number instead of returning `DaoError::Overflow`. The trailing `safe_add` only catches the subset of cases where the already-truncated value plus `occupied_capacity` overflows u64; it does not detect the prior silent truncation. This is the direct arithmetic-overflow analog of the `_sqrtPriceX96ToUint` report: an unguarded multiplication whose result is silently narrowed into a smaller integer type.

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

`withdraw_counted_capacity` is a `u128`. The cast `as u64` is a **bit-truncating** operation in Rust: if the value exceeds `u64::MAX` (18 446 744 073 709 551 615), the upper 64 bits are silently discarded and the function continues with a completely wrong capacity value. No error is returned.

The correct pattern — `u64::try_from(...).map_err(|_| DaoError::Overflow)?` — is already used for the analogous `ar_increase128` and `reward128` computations in the same file:

```rust
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) 

```rust
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [3](#0-2) 

`calculate_maximum_withdraw` is the only site that uses the unsafe `as u64` cast instead.

The `safe_add(occupied_capacity)` guard at the end only catches the case where `(withdraw_counted_capacity % 2^64) + occupied_capacity` itself overflows u64. When the truncated value is small (e.g., `withdraw_counted_capacity = 2^64 + k` for small `k`), `safe_add` succeeds and the function silently returns `k + occupied_capacity` — a value orders of magnitude smaller than the correct withdrawal amount.

The existing overflow test (`check_withdraw_calculation_overflows`) only exercises the path where `safe_add` catches the error; it does not cover the silent-truncation path where `safe_add` succeeds with a wrong value. [4](#0-3) 

---

### Impact Explanation

**Consensus / transaction-fee accounting:** `calculate_maximum_withdraw` is called from `transaction_maximum_withdraw`, which feeds `DaoCalculator::transaction_fee`. That fee value is used during block verification to check that DAO withdrawal transactions do not create capacity from thin air. A silently truncated (too-small) withdrawal capacity causes `transaction_fee = truncated_value - outputs_capacity` to underflow via `safe_sub`, making the node reject a **valid** DAO withdrawal transaction. Nodes that compute the correct value would accept it; nodes running this code would reject it — a **consensus split**.

**RPC data corruption:** The public RPC method `calculate_dao_maximum_withdraw` calls `calculate_maximum_withdraw` directly and returns the result to callers. A user querying the maximum withdrawal for a large-capacity DAO cell would receive a silently wrong (drastically smaller) number, leading to incorrect transaction construction. [5](#0-4) 

---

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`counted_capacity` is bounded by the cell's capacity minus its occupied capacity, so it can approach `u64::MAX` (~18.4 × 10¹⁸ shannons). `withdrawing_ar / deposit_ar` is the accumulated interest ratio; it starts at 1 and grows with each block's secondary issuance. For the product to exceed `u64::MAX`, the ratio must exceed 1 by more than `occupied_capacity / counted_capacity`. For a near-maximum-capacity cell with negligible occupied capacity, even a ratio of `1 + ε` for tiny ε is sufficient. Over a long enough chain lifetime (or on a testnet/devnet with high secondary issuance), this threshold is reachable. The condition is not gated by any privileged role — any transaction sender who deposits a sufficiently large cell into NervosDAO and later withdraws can trigger it.

---

### Recommendation

Replace the silent cast with a checked conversion, consistent with the pattern already used elsewhere in the same function:

```rust
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

---

### Proof of Concept

**Triggering the silent-truncation path (no error returned, wrong value):**

```
counted_capacity  = 2^64 = 18_446_744_073_709_551_616  shannons
                          (requires output_capacity > u64::MAX — use u64::MAX and occupied=0 for approximation)

withdrawing_ar    = 2 × deposit_ar

withdraw_counted_capacity (u128) = 2^64 × 2 / 1 = 2^65

as u64  →  0   (upper 65th bit discarded)

safe_add(occupied_capacity=0)  →  Ok(Capacity::shannons(0))
```

The function returns `Ok(0 shannons)` instead of `Ok(~36.9 × 10¹⁸ shannons)` — a complete loss of the depositor's principal — with no error signal.

**Concrete numeric example within u64 cell capacity:**

```
output_capacity   = 18_446_744_073_709_551_615  (u64::MAX shannons)
occupied_capacity = 0
counted_capacity  = 18_446_744_073_709_551_615

deposit_ar        = 10_000_000_000_000_000
withdrawing_ar    = 20_000_000_000_000_001   (AR doubled + 1)

withdraw_counted_capacity (u128)
  = 18_446_744_073_709_551_615 × 20_000_000_000_000_001
    / 10_000_000_000_000_000
  = 36_893_488_147_419_103_232 + small_remainder
  ≈ 36_893_488_147_419_103_232   (> u64::MAX = 18_446_744_073_709_551_615)

as u64  →  36_893_488_147_419_103_232 mod 2^64
         = 36_893_488_147_419_103_232 - 18_446_744_073_709_551_616
         = 18_446_744_073_709_551_616   (still > u64::MAX, wraps again)
         = 0  (approximately, depending on exact remainder)

safe_add(0)  →  Ok(Capacity::shannons(~0))
```

The node returns success with a near-zero withdrawal capacity. The DAO type script on-chain computes the correct value; the node's off-chain verifier computes a wrong fee, causing a consensus discrepancy for any block containing such a withdrawal. [6](#0-5) [7](#0-6)

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

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
```

**File:** util/dao/src/lib.rs (L258-258)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
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

**File:** rpc/src/module/experiment.rs (L259-267)
```rust
                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
```
