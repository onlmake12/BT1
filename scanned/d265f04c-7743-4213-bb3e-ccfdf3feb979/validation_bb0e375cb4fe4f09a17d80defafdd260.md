### Title
`CapacityVerifier` Skips `OutputsSumOverflow` Check for Any Transaction Containing a DAO Input, Allowing Capacity Inflation — (`verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::valid_dao_withdraw_transaction()` returns `true` for **any** transaction whose inputs include a cell with the DAO type script — including deposit-phase DAO cells (phase 1, data = `0x0000000000000000`). When this function returns `true`, the entire `OutputsSumOverflow` guard is skipped. Because the DAO type script only enforces capacity conservation for the DAO cell itself (not the total transaction balance), an unprivileged transaction sender can pair a DAO deposit cell with non-DAO inputs and inflate the non-DAO outputs, creating CKB capacity from nothing.

---

### Finding Description

In `CapacityVerifier::verify()`, the `OutputsSumOverflow` check is bypassed whenever `valid_dao_withdraw_transaction()` returns `true`:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(TransactionError::OutputsSumOverflow { ... }.into());
    }
}
``` [1](#0-0) 

`valid_dao_withdraw_transaction()` is defined as:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

And `cell_uses_dao_type_script` checks only the type script hash and hash type — it does **not** distinguish between a deposit-phase DAO cell (phase 1, cell data = 8 zero bytes) and a prepare/withdrawal-phase DAO cell (phase 2, cell data = deposit block number):

```rust
fn cell_uses_dao_type_script(cell_output: &CellOutput, dao_type_hash: &Byte32) -> bool {
    cell_output.type_().to_opt()
        .map(|t| {
            Into::<u8>::into(t.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
                && &t.code_hash() == dao_type_hash
        })
        .unwrap_or(false)
}
``` [3](#0-2) 

The code comment states the rationale: *"DAO withdraw transaction is verified via the type script of DAO cells."* This is only correct for phase-2 withdrawal transactions, where the DAO type script enforces the exact maximum withdrawal amount (principal + interest). For phase-1 deposit→prepare transitions, the DAO type script enforces only that the DAO cell's own capacity is preserved at the same index — it does **not** enforce that the total transaction inputs ≥ total transaction outputs. [4](#0-3) 

The DAO maximum-withdraw calculation in `DaoCalculator` confirms this: for a deposit-phase cell (`deposited_block_number == 0`), it simply returns `output.capacity()` — the original capacity — with no interest, and no enforcement of the total transaction balance:

```rust
if deposited_block_number > 0 {
    // ... interest calculation ...
} else {
    Ok(output.capacity().into())
}
``` [5](#0-4) 

---

### Impact Explanation

An unprivileged transaction sender can:

1. Own or create a DAO deposit cell (phase 1) with capacity `D`.
2. Gather non-DAO live cells with total capacity `N`.
3. Craft a transaction:
   - **Inputs**: DAO deposit cell (`D`) + non-DAO cells (`N`)
   - **Outputs**: DAO prepare cell (`D`, same capacity — passes DAO type script) + non-DAO cells (`N + X`, where `X > 0`)
4. `valid_dao_withdraw_transaction()` returns `true` (there is a DAO input).
5. `OutputsSumOverflow` check is skipped entirely.
6. The DAO type script validates only the DAO cell's capacity (`D → D`), not the total.
7. The transaction is accepted with `X` CKB created from nothing.

This breaks the fundamental capacity conservation invariant of CKB's UTXO model, allowing arbitrary inflation of the CKB token supply by any transaction sender who holds a DAO deposit cell.

---

### Likelihood Explanation

The attack requires only that the attacker hold a DAO deposit cell, which any user can create permissionlessly by sending a standard deposit transaction. No privileged role, leaked key, or majority hashpower is required. The entry path is a standard RPC `send_transaction` call. The attack is deterministic and repeatable.

---

### Recommendation

`valid_dao_withdraw_transaction()` must be restricted to only match **phase-2 withdrawal** transactions — i.e., transactions where the DAO input cell's data encodes a non-zero deposit block number. The function should inspect the cell data of each DAO-typed input and return `true` only if at least one such input has `deposited_block_number > 0`:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| {
            if !cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash) {
                return false;
            }
            // Only skip the capacity check for phase-2 (prepare→withdraw) cells,
            // where deposited_block_number > 0 in the cell data.
            cell_meta.mem_cell_data
                .as_ref()
                .filter(|data| data.len() == 8)
                .map(|data| LittleEndian::read_u64(data) > 0)
                .unwrap_or(false)
        })
}
```

For phase-1 (deposit→prepare) transactions, the standard `OutputsSumOverflow` check must remain active, since the DAO type script does not enforce total transaction capacity balance in that phase.

---

### Proof of Concept

```
1. Alice creates a DAO deposit cell: capacity = 1000 CKB, data = 0x0000000000000000
   (This is a standard DAO deposit, accepted by the network.)

2. Alice also holds a non-DAO live cell: capacity = 500 CKB.

3. Alice crafts a transaction:
   Inputs:
     - DAO deposit cell (1000 CKB, DAO type script, data = 0x0000000000000000)
     - Non-DAO cell (500 CKB)
   Outputs:
     - DAO prepare cell (1000 CKB, DAO type script, data = current_block_number_le)
     - Non-DAO cell (600 CKB)   ← 100 CKB inflated

4. Verification path:
   - CapacityVerifier::valid_dao_withdraw_transaction() → true
     (because the DAO deposit cell has the DAO type script)
   - OutputsSumOverflow check is SKIPPED (total inputs=1500, total outputs=1600)
   - DAO type script runs: checks DAO input (1000) → DAO output (1000) ✓
   - Non-DAO output (600) is never checked against non-DAO input (500)

5. Transaction is accepted. Alice has created 100 CKB from nothing.
   Repeating this with larger amounts or in parallel yields unbounded inflation.
``` [6](#0-5) [7](#0-6)

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

**File:** util/dao/src/lib.rs (L58-116)
```rust
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
```
