### Title
Silent Truncating Cast in DAO Withdrawal Capacity Arithmetic Corrupts DAO State Field - (File: `util/dao/src/lib.rs`)

### Summary
In `DaoCalculator::calculate_maximum_withdraw`, the intermediate `u128` result of the ar-scaled capacity computation is cast to `u64` using a silent truncating `as u64` cast instead of the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern used everywhere else in the same file. If the scaled result exceeds `u64::MAX`, the cast silently wraps to a small value, producing a wrong (drastically underestimated) withdrawal capacity with no error returned. This wrong value propagates into the DAO state field `S` via `withdrawed_interests`, corrupting on-chain DAO accounting.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the ar-scaled withdrawal capacity in `u128` to avoid intermediate overflow, then casts back to `u64`:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently discards the upper 64 bits if `withdraw_counted_capacity > u64::MAX`. This is structurally identical to the FujiERC1155 bug: a scaled value is computed correctly in a wider type, but the wrong (truncated) value is then used to update accounting state.

Every other analogous `u128 → u64` narrowing in the same file uses the checked pattern:

```rust
// secondary_block_reward (line 204)
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch (line 245)
let miner_issuance = Capacity::shannons(
    u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?
);

// dao_field_with_current_epoch (line 258)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

Only `calculate_maximum_withdraw` uses the unsafe `as u64` cast.

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`counted_capacity = output_capacity − occupied_capacity` is bounded by the cell's declared capacity (a `u64`). `withdrawing_ar / deposit_ar` is the ratio of the accumulation rate at withdrawal time to the rate at deposit time. Because `ar` only increases, any cell deposited when `ar` was low and withdrawn when `ar` has grown sufficiently can trigger this.

### Impact Explanation

`calculate_maximum_withdraw` is called from three paths:

1. **`withdrawed_interests` → `dao_field_with_current_epoch`** — the DAO state field `S` (secondary issuance accumulated in the DAO) is updated as:
   ```
   current_s = parent_s + nervosdao_issuance − withdrawed_interests
   ```
   If `calculate_maximum_withdraw` returns a truncated (too-small) value, `withdrawed_interests` is understated, so `current_s` is overstated. Both the block producer and the `DaoHeaderVerifier` use the same `DaoCalculator::dao_field` path, so the corrupted `S` value passes consensus verification and is committed to the chain. Subsequent DAO withdrawals that rely on `S` for interest accounting are affected. [5](#0-4) [6](#0-5) 

2. **`transaction_fee`** — the tx-pool uses this to compute the fee for DAO withdrawal transactions. A truncated result causes the fee to be computed as a large positive number (since `maximum_withdraw` is understated, `fee = maximum_withdraw − outputs_capacity` can underflow and `safe_sub` returns an error, causing the transaction to be rejected from the pool even though it is valid). [7](#0-6) 

3. **`calculate_dao_maximum_withdraw` RPC** — returns a wrong value to any caller, causing users to construct transactions with incorrect output capacities. [8](#0-7) 

### Likelihood Explanation

The overflow requires `counted_capacity × (withdrawing_ar / deposit_ar) > u64::MAX`. The total CKB issuance is approximately 33.6 billion CKB = ~3.36 × 10¹⁸ shannons, well below `u64::MAX ≈ 1.84 × 10¹⁹`. For the overflow to trigger, `ar` must grow by a factor of approximately `u64::MAX / max_counted_capacity ≈ 5.5×` relative to the deposit-time `ar`. At the current secondary issuance rate, this would take many decades. Likelihood is therefore **low** under current economic parameters, but the defect is a latent time-bomb: as `ar` grows monotonically and never resets, the window of exploitability widens with every block.

The entry path is fully unprivileged: any transaction sender who holds a DAO deposit cell and submits a phase-2 withdrawal transaction triggers this code path.

### Recommendation

Replace the silent truncating cast with the checked conversion already used everywhere else in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (consistent with rest of file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
``` [9](#0-8) 

### Proof of Concept

Trigger conditions (arithmetic):

```
deposit_ar  = 10_000_000_000_000_000   (initial genesis ar)
withdrawing_ar = 55_000_000_000_000_000  (ar after ~5.5× growth)
counted_capacity = 3_360_000_000_000_000_000  (max realistic cell ~33.6B CKB)

withdraw_counted_capacity (u128)
  = 3_360_000_000_000_000_000 × 55_000_000_000_000_000
    / 10_000_000_000_000_000
  = 18_480_000_000_000_000_000   >  u64::MAX (18_446_744_073_709_551_615)

withdraw_counted_capacity as u64
  = 18_480_000_000_000_000_000 mod 2^64
  = 33_255_926_290_448_384          ← drastically wrong small value

withdraw_capacity returned = 33_255_926_290_448_384 + occupied_capacity
                           ≈ 33_255_930_390_448_384 shannons  (~332 CKB)
```

Instead of the correct ~184 billion CKB, the function returns ~332 CKB with no error. The `DaoHeaderVerifier` accepts the block because both producer and verifier run the same buggy `dao_field` computation, committing an inflated `S` to the chain. [10](#0-9) [11](#0-10)

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

**File:** util/dao/src/lib.rs (L146-158)
```rust
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

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
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
