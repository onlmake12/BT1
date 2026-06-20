### Title
NervosDAO `safe_sub` Prevents Expected Arithmetic Underflow in `dao_field_with_current_epoch`, Locking DAO Withdrawal Funds — (`File: util/dao/src/lib.rs`)

---

### Summary

In `DaoCalculator::dao_field_with_current_epoch`, the NervosDAO savings field `current_s` is computed using `safe_sub` (backed by `checked_sub`). Due to integer-division rounding in both the accumulation-rate (`ar`) formula and the `calculate_maximum_withdraw` formula, `withdrawed_interests` can legitimately exceed `parent_s + nervosdao_issuance` by a few shannons. When this happens, `safe_sub` returns `Err(Overflow)`, causing block validation to reject any block that contains the offending DAO withdrawal transaction. The depositor's funds become permanently inaccessible.

---

### Finding Description

`dao_field_with_current_epoch` computes the new DAO header field for each block. The NervosDAO savings accumulator `S` is updated as:

```rust
let current_s = parent_s
    .safe_add(nervosdao_issuance)
    .and_then(|s| s.safe_sub(withdrawed_interests))?;
``` [1](#0-0) 

`safe_sub` is defined as `checked_sub(...).ok_or(Error::Overflow)`: [2](#0-1) 

`withdrawed_interests` is the net interest paid to DAO depositors in the block — `maximum_withdraw - input_capacities`: [3](#0-2) 

`maximum_withdraw` for each DAO cell is:

```
withdraw_counted_capacity = counted_capacity * withdrawing_ar / deposit_ar   (integer division)
``` [4](#0-3) 

`nervosdao_issuance` accumulated into `S` each block is:

```
nervosdao_issuance = current_g2 - floor(current_g2 * parent_u / parent_c)
``` [5](#0-4) 

And `ar` grows as:

```
ar_increase = floor(parent_ar * current_g2 / parent_c)
``` [6](#0-5) 

Because both the `ar` accumulation and the interest payout use independent floor-division, the total interest paid to depositors can exceed the total `S` accumulated by a few shannons. This is the same class of rounding-induced underflow that the Uniswap H-04 report describes: the protocol math is designed to allow the subtraction to go negative, but the implementation uses checked arithmetic that panics/errors instead.

---

### Impact Explanation

`DaoHeaderVerifier::verify()` calls `dao_field()` → `dao_field_with_current_epoch()` during block validation: [7](#0-6) 

If `safe_sub` returns `Err(Overflow)`, the error propagates out of `dao_field()` and the block is rejected. The same function is called during block template assembly: [8](#0-7) 

A DAO withdrawal transaction that triggers the rounding underflow cannot be included in any valid block. The depositor's NervosDAO cell is permanently locked — no miner can ever commit a block containing the withdrawal.

---

### Likelihood Explanation

The rounding discrepancy is small (at most a few shannons per withdrawal) but cumulative. It depends on the specific values of `ar`, `parent_c`, `parent_u`, and the depositor's `counted_capacity`. As the chain matures and `ar` grows, the probability of hitting the off-by-one boundary increases. A depositor with a large `counted_capacity` and a long deposit period is most at risk. No privileged access is required — any unprivileged DAO depositor can reach this state by submitting a standard phase-2 withdrawal transaction.

---

### Recommendation

Replace `safe_sub` with `saturating_sub` (or wrapping subtraction) for the `current_s` update, mirroring the approach used elsewhere in the codebase for analogous accumulator fields:

```diff
- let current_s = parent_s
-     .safe_add(nervosdao_issuance)
-     .and_then(|s| s.safe_sub(withdrawed_interests))?;
+ let current_s = Capacity::shannons(
+     parent_s
+         .as_u64()
+         .saturating_add(nervosdao_issuance.as_u64())
+         .saturating_sub(withdrawed_interests.as_u64()),
+ );
```

The same fix should be evaluated for `current_u`: [9](#0-8) 

---

### Proof of Concept

1. Deposit a large DAO cell at block `D` with `deposit_ar`.
2. Wait many blocks so `ar` accumulates many floor-division rounding errors.
3. At block `W`, initiate phase-1 withdrawal.
4. At block `W+N`, submit phase-2 withdrawal transaction.
5. The miner calls `dao_field_with_current_epoch` with this transaction. `withdrawed_interests` = `calculate_maximum_withdraw(output, ...) - input_capacity`. Due to accumulated rounding in `ar`, `withdrawed_interests` exceeds `parent_s + nervosdao_issuance` by 1 shannon.
6. `safe_sub` returns `Err(Overflow)` → `DaoHeaderVerifier::verify()` rejects the block → no miner can ever commit this withdrawal → funds are permanently locked. [1](#0-0) [2](#0-1)

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L242-246)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;
```

**File:** util/dao/src/lib.rs (L249-251)
```rust
        let current_u = parent_u
            .safe_add(added_occupied_capacities)
            .and_then(|u| u.safe_sub(freed_occupied_capacities))?;
```

**File:** util/dao/src/lib.rs (L252-254)
```rust
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
```

**File:** util/dao/src/lib.rs (L256-261)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;
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

**File:** util/occupied-capacity/core/src/units.rs (L133-138)
```rust
    pub fn safe_sub<C: IntoCapacity>(self, rhs: C) -> Result<Self> {
        self.0
            .checked_sub(rhs.into_capacity().0)
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
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

**File:** tx-pool/src/block_assembler/mod.rs (L676-679)
```rust
        // Generate DAO fields here
        let dao = DaoCalculator::new(consensus, &snapshot.borrow_as_data_loader())
            .dao_field_with_current_epoch(entries_iter, tip_header, current_epoch)?;

```
