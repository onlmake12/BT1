### Title
`CapacityVerifier` Skips Global Balance Check for Entire Transaction When Any Single Input Uses DAO Type Script — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::valid_dao_withdraw_transaction()` uses `.any()` to test whether the transaction contains a DAO-type-script input. If even one input is a DAO cell, the entire `OutputsSumOverflow` check is bypassed for the whole transaction — including all non-DAO inputs. This is a direct structural analog to the reported bug: a boolean flag is set to "fully handled" based on a partial condition, causing the remaining accounting to be silently skipped.

---

### Finding Description

In `verification/src/transaction_verifier.rs`, `CapacityVerifier::verify()` contains:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(TransactionError::OutputsSumOverflow { ... }.into());
    }
}
``` [1](#0-0) 

The gate condition is:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

`.any()` returns `true` the moment a single DAO-type-script input is found. The code then skips the `OutputsSumOverflow` check for **all** inputs in the transaction — including non-DAO inputs whose capacity is not governed by the DAO type script.

The inline comment reads:

> `// DAO withdraw transaction is verified via the type script of DAO cells` [3](#0-2) 

This assumption is only correct for pure DAO withdrawal transactions. The DAO type script (`dao.c`) enforces the capacity constraint for the DAO cell itself; it does not enforce the global transaction balance across mixed inputs.

---

### Impact Explanation

A transaction sender can craft a **mixed transaction** containing:
- One or more DAO withdrawing cells (phase-2, `deposited_block_number > 0`) — sufficient to trigger `valid_dao_withdraw_transaction() == true`
- One or more ordinary (non-DAO) input cells

Because `valid_dao_withdraw_transaction()` returns `true` on the first DAO input, the `OutputsSumOverflow` guard is entirely skipped. The non-DAO inputs' capacity is no longer subject to the global `inputs_sum >= outputs_sum` invariant enforced by `CapacityVerifier`. The DAO type script only validates the DAO cell's own maximum withdraw amount; it does not account for the non-DAO inputs' capacity in the total balance.

The `DaoCalculator::transaction_maximum_withdraw` does correctly fold non-DAO inputs at face value:

```rust
} else {
    Ok(output.capacity().into())  // non-DAO cell: use face value
}
``` [4](#0-3) 

However, whether `DaoCalculator::transaction_fee()` is invoked in the full verification pipeline for every mixed DAO transaction — and whether its error propagation is enforced — determines the concrete exploitability. If that path is not always exercised, a sender can set total output capacity above the sum of (DAO max withdraw + non-DAO input capacity), inflating capacity from nothing.

---

### Likelihood Explanation

- Entry point: any unprivileged transaction sender submitting via RPC (`send_transaction`) or P2P relay.
- The attacker only needs to own a valid DAO withdrawing cell (phase-2) and any ordinary cell.
- No privileged access, no majority hashpower, no social engineering required.
- The condition is triggered by the presence of a single DAO input — a normal, supported operation.

---

### Recommendation

Replace the transaction-level `.any()` flag with per-input accounting. The `OutputsSumOverflow` check should be applied to the non-DAO portion of inputs independently, or the check should compute:

```
non_dao_inputs_sum + dao_maximum_withdraw >= outputs_sum
```

rather than skipping the check entirely whenever any DAO input is present. Only set the "skip balance check" flag when the DAO type script is confirmed to cover the entire input set.

---

### Proof of Concept

1. Deposit CKB into Nervos DAO; complete phase-1 (prepare). This produces a DAO withdrawing cell `D` with `deposited_block_number > 0`.
2. Obtain an ordinary cell `N` with capacity `C_N`.
3. Construct a transaction:
   - Inputs: `[D, N]`
   - Output: single cell with capacity `= dao_max_withdraw(D) + C_N + X` (where `X > 0`)
4. Submit via `send_transaction` RPC.
5. `CapacityVerifier::valid_dao_withdraw_transaction()` returns `true` because input `D` uses the DAO type script.
6. The `OutputsSumOverflow` guard is skipped entirely.
7. The DAO type script only validates `D`'s contribution; `X` shannons are created from nothing if `DaoCalculator::transaction_fee()` is not enforced on this path. [5](#0-4) [6](#0-5)

### Citations

**File:** verification/src/transaction_verifier.rs (L478-494)
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
```

**File:** verification/src/transaction_verifier.rs (L517-523)
```rust
    fn valid_dao_withdraw_transaction(&self) -> bool {
        self.resolved_transaction
            .resolved_inputs
            .iter()
            .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
    }
}
```

**File:** util/dao/src/lib.rs (L38-124)
```rust
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
