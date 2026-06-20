### Title
`CapacityVerifier::valid_dao_withdraw_transaction` Uses `.any()` to Skip the Entire `OutputsSumOverflow` Check, Allowing Non-DAO Capacity Inflation in Mixed Transactions — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for the **entire transaction** whenever any single input cell carries the DAO type script. Because the DAO type script itself only validates the DAO-specific withdrawal amount and not the total transaction capacity balance, an attacker can include one legitimate DAO input alongside arbitrary non-DAO inputs and set outputs that exceed the combined input capacity, minting CKB out of thin air.

---

### Finding Description

In `verification/src/transaction_verifier.rs`, the `CapacityVerifier::verify()` method guards against outputs exceeding inputs:

```rust
pub fn verify(&self) -> Result<(), Error> {
    if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
        let inputs_sum = self.resolved_transaction.inputs_capacity()?;
        let outputs_sum = self.resolved_transaction.outputs_capacity()?;
        if inputs_sum < outputs_sum {
            return Err((TransactionError::OutputsSumOverflow { ... }).into());
        }
    }
    ...
}
``` [1](#0-0) 

The bypass condition is:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

The `.any()` predicate returns `true` if **even one** input carries the DAO type script. When it does, the `OutputsSumOverflow` guard is skipped for the **whole transaction** — including all non-DAO inputs and outputs.

The code comment states: *"DAO withdraw transaction is verified via the type script of DAO cells."* However, the DAO type script (`util/dao/src/lib.rs`) only computes and validates the withdrawal amount for DAO-tagged cells:

```rust
if is_dao_output {
    ...
    self.calculate_maximum_withdraw(output, ...)
} else {
    Ok(output.capacity().into())  // non-DAO inputs: just return face value
}
``` [3](#0-2) 

The DAO type script does not enforce that `total_outputs_capacity ≤ total_inputs_capacity`. That invariant is exclusively the responsibility of `CapacityVerifier`, which has been bypassed.

A grep across `verification/contextual/src/` confirms there is no secondary `OutputsSumOverflow` check anywhere else in the contextual block verifier pipeline.

---

### Impact Explanation

An attacker who controls a valid DAO withdrawal transaction (Phase 2) can craft a mixed transaction:

| Cell | Capacity |
|---|---|
| DAO input (Phase 2 withdraw cell) | 102 CKB (+ interest) |
| Non-DAO input | 1,000 CKB |
| Output 1 (satisfies DAO type script) | 102.001 CKB |
| Output 2 (attacker-controlled) | 1,100 CKB |

- **Total inputs:** 1,102 CKB  
- **Total outputs:** 1,202.001 CKB  
- **Inflated:** ~100 CKB created from nothing

`CapacityVerifier` skips the overflow check because the DAO input triggers `valid_dao_withdraw_transaction() == true`. The DAO type script approves Output 1 as a valid withdrawal. Output 2 is never checked by any verifier. The block is accepted by consensus, permanently inflating the CKB supply.

This is a **consensus-level, chain-wide** impact: every honest node accepts the block, the inflated capacity is committed to the canonical chain, and the attacker receives spendable CKB cells with no corresponding inputs.

---

### Likelihood Explanation

The attacker needs only:
1. A valid DAO deposit (minimum ~102 CKB, publicly available to any CKB holder).
2. To wait the DAO lock period (~30 days on mainnet).
3. To craft a Phase 2 withdrawal transaction with extra non-DAO inputs and inflated outputs.

No privileged access, no majority hashpower, no social engineering. The entry path is the standard `send_transaction` RPC or direct block submission by a miner. Any CKB holder can execute this.

---

### Recommendation

Replace the coarse `.any()` bypass with a precise, per-cell accounting approach. The `OutputsSumOverflow` check should never be skipped wholesale. Instead:

1. Compute `non_dao_inputs_sum` (sum of capacity of all non-DAO inputs).
2. Compute `dao_maximum_withdraw_sum` (sum of calculated withdrawal amounts for all DAO inputs, using `DaoCalculator`).
3. Assert `total_outputs_capacity ≤ non_dao_inputs_sum + dao_maximum_withdraw_sum`.

This mirrors the correct fix suggested in M-43: check the combined balance of the specific restricted resource, not just the presence of a flag that disables the check entirely.

---

### Proof of Concept

**Step 1 — Deposit into DAO:**  
Submit a standard DAO deposit transaction locking 102 CKB into a cell with the DAO type script.

**Step 2 — Phase 1 Withdraw:**  
After some blocks, submit Phase 1 (prepare) transaction. Record the block hash.

**Step 3 — Craft malicious Phase 2 transaction:**

```
inputs:
  [0] DAO Phase-1 cell  (102 CKB, DAO type script, valid witness with deposit header index)
  [1] Regular cell      (1,000 CKB, no type script)

outputs:
  [0] Regular cell      (102.001 CKB)   ← satisfies DAO type script check
  [1] Regular cell      (1,100 CKB)     ← 100 CKB inflated, never checked

header_deps: [deposit_block_hash, withdraw_block_hash]
```

**Step 4 — Submit:**  
Call `send_transaction` RPC. `CapacityVerifier::valid_dao_withdraw_transaction()` returns `true` (input[0] has DAO type script), so the `OutputsSumOverflow` guard is skipped. The DAO type script executes and approves input[0]'s withdrawal. No verifier checks input[1] vs output[1]. The transaction is accepted.

**Step 5 — Result:**  
Attacker receives output[1] with 1,100 CKB despite only providing 1,000 CKB in non-DAO inputs — 100 CKB minted from nothing, committed to the canonical chain. [4](#0-3)

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

**File:** util/dao/src/lib.rs (L58-119)
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
                    } else {
                        Ok(output.capacity().into())
                    }
```
