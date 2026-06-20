### Title
Overly Broad DAO Withdraw Guard in `CapacityVerifier` Skips Capacity Overflow Check for Entire Mixed Transaction — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::valid_dao_withdraw_transaction()` uses `.any()` to test whether **any** resolved input carries a DAO type script. When it returns `true`, the entire `OutputsSumOverflow` check is suppressed for the transaction — including for all non-DAO inputs and outputs. Because the DAO type script only validates DAO-specific cells, the capacity balance of non-DAO cells in the same transaction is left unchecked by `CapacityVerifier`.

---

### Finding Description

In `verification/src/transaction_verifier.rs`, `CapacityVerifier::verify()` conditionally skips the `OutputsSumOverflow` guard:

```rust
// line 483
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(...OutputsSumOverflow...);
    }
}
```

The predicate that triggers the skip is:

```rust
// lines 517-522
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
```

`.any()` returns `true` the moment a **single** input carries the DAO type script — regardless of how many non-DAO inputs are also present. The entire `OutputsSumOverflow` check is then suppressed for the whole transaction.

The DAO type script (running in CKB-VM) only validates the DAO-specific cells (deposit/prepare/withdraw amounts). It does not enforce the global capacity balance across non-DAO inputs and outputs. The comment at line 479–482 acknowledges this split responsibility:

> *"DAO withdraw transaction is verified via the type script of DAO cells"*

But that statement is only true for the DAO cells themselves — not for co-mingled non-DAO cells.

The analog to the external report is direct: just as `pool_x_account` / `pool_y_account` were checked with the wrong constraint (`owner_y_account.owner == owner.key()` instead of verifying they are actual pool reserve accounts), here the wrong predicate (`.any()` over all inputs) is used instead of a per-cell or phase-specific check, causing the wrong scope of validation to be bypassed.

---

### Impact Explanation

A transaction sender who controls a valid DAO cell (even a minimal deposit cell) can construct a transaction that:

1. Includes the DAO cell as one input alongside multiple non-DAO inputs.
2. Includes non-DAO outputs whose total capacity exceeds the non-DAO inputs' total capacity.
3. Passes `CapacityVerifier` because `valid_dao_withdraw_transaction()` returns `true` and the `OutputsSumOverflow` check is suppressed.
4. Has the DAO type script validate only the DAO-specific cells (which are correct), leaving the non-DAO capacity overflow undetected at this layer.

The `DaoCalculator::transaction_fee` (used in `tx-pool/src/util.rs` and `verification/contextual/src/contextual_block_verifier.rs`) sums both DAO and non-DAO input capacities and would produce a negative fee for such a transaction, providing a secondary defense. However, `CapacityVerifier` is the primary, non-contextual guard and its bypass represents a missing defense-in-depth layer. If any code path invokes `CapacityVerifier` without the subsequent contextual `DaoCalculator` check (e.g., isolated non-contextual verification, future refactors, or light-client verification paths), the overflow goes entirely undetected.

---

### Likelihood Explanation

Any unprivileged transaction sender who holds a DAO deposit cell — a normal, permissionless on-chain action — can craft such a transaction. No special privilege, key leak, or majority hashpower is required. The attacker only needs to submit a transaction via the standard RPC (`send_transaction`) or P2P relay.

---

### Recommendation

Replace the `.any()` predicate with a per-cell capacity accounting approach:

- Compute `inputs_sum` and `outputs_sum` for **non-DAO** inputs/outputs separately.
- Apply the `OutputsSumOverflow` check to the non-DAO portion unconditionally.
- Allow the DAO type script to govern only the DAO-cell portion of the capacity balance.

Alternatively, tighten `valid_dao_withdraw_transaction()` to only return `true` when the transaction is exclusively a DAO phase-3 withdrawal (all inputs are prepared DAO cells with non-zero `deposited_block_number` data), rather than triggering on any DAO input.

---

### Proof of Concept

```
Inputs:
  [0] DAO deposit cell  — capacity: 100 CKB  (data = 0x00..00, DAO type script)
  [1] Normal cell       — capacity: 1000 CKB (no type script)

Outputs:
  [0] Normal cell       — capacity: 101 CKB  (DAO withdrawal with interest, correct per DAO script)
  [1] Normal cell       — capacity: 1500 CKB (attacker-controlled, 500 CKB excess)

CapacityVerifier:
  valid_dao_withdraw_transaction() → true  (input[0] has DAO type script)
  OutputsSumOverflow check → SKIPPED entirely

DAO type script:
  Validates input[0] → output[0] withdrawal: OK (101 CKB is valid interest)
  Does not inspect input[1] / output[1] capacity balance

Result at CapacityVerifier layer:
  Non-DAO overflow of 500 CKB (1500 - 1000) is never checked.
``` [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** verification/src/transaction_verifier.rs (L478-523)
```rust
    pub fn verify(&self) -> Result<(), Error> {
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

        for (index, (output, data)) in self
            .resolved_transaction
            .transaction
            .outputs_with_data_iter()
            .enumerate()
        {
            let data_occupied_capacity = Capacity::bytes(data.len())?;
            if output.is_lack_of_capacity(data_occupied_capacity)? {
                return Err((TransactionError::InsufficientCellCapacity {
                    index,
                    inner: TransactionErrorSource::Outputs,
                    capacity: output.capacity().into(),
                    occupied_capacity: output.occupied_capacity(data_occupied_capacity)?,
                })
                .into());
            }
        }

        Ok(())
    }

    fn valid_dao_withdraw_transaction(&self) -> bool {
        self.resolved_transaction
            .resolved_inputs
            .iter()
            .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
    }
}
```

**File:** verification/src/transaction_verifier.rs (L525-534)
```rust
fn cell_uses_dao_type_script(cell_output: &CellOutput, dao_type_hash: &Byte32) -> bool {
    cell_output
        .type_()
        .to_opt()
        .map(|t| {
            Into::<u8>::into(t.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
                && &t.code_hash() == dao_type_hash
        })
        .unwrap_or(false)
}
```

**File:** util/dao/src/lib.rs (L28-124)
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

    fn transaction_maximum_withdraw(
        &self,
        rtx: &ResolvedTransaction,
    ) -> Result<Capacity, DaoError> {
        let header_deps: HashSet<Byte32> = rtx.transaction.header_deps_iter().collect();
        rtx.resolved_inputs.iter().enumerate().try_fold(
            Capacity::zero(),
            |capacities, (i, cell_meta)| {
                let capacity: Result<Capacity, DaoError> = {
                    let output = &cell_meta.cell_output;
                    let is_dao_type_script = |type_script: Script| {
                        Into::<u8>::into(type_script.hash_type())
                            == Into::<u8>::into(ScriptHashType::Type)
                            && type_script.code_hash() == self.consensus.dao_type_hash()
                    };
                    let is_dao_output = output
                        .type_()
                        .to_opt()
                        .map(is_dao_type_script)
                        .unwrap_or(false);
                    if is_dao_output {
                        // A withdrawing DAO cell has 8 bytes of cell data storing the
                        // block number of the original deposit.
                        let deposited_block_number =
                            match self.data_loader.load_cell_data(cell_meta) {
                                Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
                                _ => 0,
                            };
                        if deposited_block_number > 0 {
                            let withdrawing_header_hash = cell_meta
                                .transaction_info
                                .as_ref()
                                .map(|info| &info.block_hash)
                                .filter(|hash| header_deps.contains(hash))
                                .ok_or(DaoError::InvalidOutPoint)?;
                            let deposit_header_hash = rtx
                                .transaction
                                .witnesses()
                                .get(i)
                                .ok_or(DaoError::InvalidOutPoint)
                                .and_then(|witness_data| {
                                    // dao contract stores header deps index as u64 in the input_type field of WitnessArgs
                                    let witness =
                                        WitnessArgs::from_slice(&Into::<Bytes>::into(witness_data))
                                            .map_err(|_| DaoError::InvalidDaoFormat)?;
                                    let header_deps_index_data: Option<Bytes> =
                                        witness.input_type().to_opt().map(|witness| witness.into());
                                    if header_deps_index_data.is_none()
                                        || header_deps_index_data.clone().map(|data| data.len())
                                            != Some(8)
                                    {
                                        return Err(DaoError::InvalidDaoFormat);
                                    }
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
                                        .and_then(|hash| header_deps.get(&hash))
                                        .ok_or(DaoError::InvalidOutPoint)
                                })?;

                            let deposit_header = self
                                .data_loader
                                .get_header(deposit_header_hash)
                                .ok_or(DaoError::InvalidHeader)?;
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
                        } else {
                            Ok(output.capacity().into())
                        }
                    } else {
                        Ok(output.capacity().into())
                    }
                };
                capacity.and_then(|c| c.safe_add(capacities).map_err(Into::into))
            },
        )
    }
```
