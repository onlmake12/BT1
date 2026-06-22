### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Violates Withdrawal Invariant — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes `withdraw_counted_capacity` as a `u128` intermediate value and then silently truncates it to `u64` via an unchecked `as u64` cast. Every other `u128`→`u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The missing post-condition check means that if the product overflows `u64::MAX`, the returned withdrawal capacity is silently smaller than the original deposit — violating the invariant `withdraw_capacity >= output_capacity` — with no error surfaced to the caller.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the DAO withdrawal amount as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently discards the upper 64 bits if `withdraw_counted_capacity > u64::MAX`. Compare this with every other `u128`→`u64` conversion in the same file, all of which use the checked form:

```rust
// line 244-245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) 

After the truncation there is **no assertion** that `withdraw_capacity >= output_capacity`, i.e. that the withdrawal amount is at least the original deposit. The invariant is mathematically guaranteed only when `withdraw_counted_capacity` fits in `u64`; when it does not, the invariant silently breaks.

`calculate_maximum_withdraw` feeds three downstream paths:

1. **`transaction_maximum_withdraw` → `transaction_fee`** — used by the tx-pool and `RewardCalculator` to compute miner fees.
2. **`withdrawed_interests` → `dao_field_with_current_epoch`** — the DAO field written into every block header, verified consensus-critically by `DaoHeaderVerifier`.
3. **RPC `calculate_dao_maximum_withdraw`** — exposed to any caller. [3](#0-2) [4](#0-3) [5](#0-4) 

---

### Impact Explanation

If `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`, the silent truncation produces a `withdraw_capacity` that is smaller than `output_capacity`. This causes:

- **`transaction_fee`** to underflow (`maximum_withdraw.safe_sub(outputs_capacity)` returns `Err`), causing the DAO withdrawal transaction to be rejected from the tx-pool even though it is valid.
- **`withdrawed_interests`** to return an incorrect (too-small) value, making `current_s` in the DAO field too large. The block's DAO field will not match the value computed by `DaoHeaderVerifier`, causing the block to be rejected with `BlockErrorKind::InvalidDAO`.

The net effect is a **consensus-level denial of service**: any block containing a DAO withdrawal whose `withdraw_counted_capacity` overflows `u64` will be permanently rejected by all honest nodes, even though the withdrawal is economically valid.

---

### Likelihood Explanation

The overflow condition requires `counted_capacity * withdrawing_ar > u64::MAX * deposit_ar`. Since `ar` starts at `10^16` and grows by approximately `parent_ar * g2 / C` per block (where `g2 ≪ C` on mainnet), `ar` doubles only after an astronomically large number of blocks. For a deposit of `u64::MAX` shannons (~184 billion CKB), the overflow would require `ar` to exceed `2 * 10^16`, which takes on the order of `10^12` blocks at current issuance rates — effectively unreachable on mainnet today.

However, the code defect is real and structurally inconsistent with the rest of the file. On a chain with higher secondary issuance parameters (e.g., a testnet or a chain spec with elevated `secondary_epoch_reward`), the threshold is reached much sooner. Any transaction sender who deposits a sufficiently large amount can trigger the condition once `ar` grows enough.

---

### Recommendation

Replace the silent `as u64` cast with the same checked conversion used everywhere else in the file:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;

// Post-condition: withdrawal must not be less than the original deposit
debug_assert!(withdraw_capacity >= output_capacity);
```

This makes the overflow explicit (returns `DaoError::Overflow` instead of silently corrupting the result) and aligns with the invariant-checking pattern used for `miner_issuance128` and `ar_increase128`. [6](#0-5) 

---

### Proof of Concept

```rust
// Construct a scenario where withdraw_counted_capacity overflows u64:
// counted_capacity = u64::MAX = 18_446_744_073_709_551_615
// deposit_ar       = 10_000_000_000_000_000   (initial ar)
// withdrawing_ar   = 20_000_000_000_000_001   (ar has slightly more than doubled)
//
// withdraw_counted_capacity (u128) =
//   18_446_744_073_709_551_615 * 20_000_000_000_000_001 / 10_000_000_000_000_000
//   ≈ 36_893_488_147_419_103_232   (> u64::MAX)
//
// withdraw_counted_capacity as u64 = 36_893_488_147_419_103_232 - 2^64
//                                  = 18_446_744_073_709_551_616  -- wraps to 1
//
// Result: withdraw_capacity = 1 + occupied_capacity
//         which is << output_capacity  (invariant violated, no error returned)
```

The `CapacityVerifier` explicitly skips the `inputs_sum >= outputs_sum` check for DAO withdraw transactions, relying on the DAO type script and `DaoCalculator` to enforce correctness. The silent truncation bypasses both. [7](#0-6)

### Citations

**File:** util/dao/src/lib.rs (L28-36)
```rust
impl<'a, DL: CellDataProvider + HeaderProvider> DaoCalculator<'a, DL> {
    /// Returns the total transactions fee of `rtx`.
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

**File:** util/dao/src/lib.rs (L242-258)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;

        let current_c = parent_c.safe_add(current_g)?;
        let current_u = parent_u
            .safe_add(added_occupied_capacities)
            .and_then(|u| u.safe_sub(freed_occupied_capacities))?;
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;

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

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-320)
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
    }
```

**File:** verification/src/transaction_verifier.rs (L479-494)
```rust
        // skip OutputsSumOverflow verification for resolved cellbase and DAO
        // withdraw transactions.
        // cellbase's outputs are verified by RewardVerifier
        // DAO withdraw transaction is verified via the type script of DAO cells
        if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
            let inputs_sum = self.resolved_transaction.inputs_capacity()?;
            let outputs_sum = self.resolved_transaction.outputs_capacity()?;

            if inputs_sum < outputs_sum {
                return Err((TransactionError::OutputsSumOverflow {
                    inputs_sum,
                    outputs_sum,
                })
                .into());
            }
        }
```
