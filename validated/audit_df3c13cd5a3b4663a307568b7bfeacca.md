### Title
`CapacityVerifier` Skips Entire Capacity Check for Mixed DAO+Non-DAO Input Transactions, Allowing CKB Inflation - (File: `verification/src/transaction_verifier.rs`)

### Summary

`CapacityVerifier::valid_dao_withdraw_transaction()` returns `true` if **any** input uses the DAO type script, causing the entire `OutputsSumOverflow` check to be skipped for the whole transaction. The DAO type script (per RFC 0023) only verifies capacity per DAO cell, not the total transaction balance. For a mixed transaction containing both DAO and non-DAO inputs, neither the `CapacityVerifier` nor the DAO type script checks the non-DAO inputs' capacity, allowing an attacker to inflate non-DAO outputs beyond non-DAO inputs and create CKB from nothing.

### Finding Description

In `CapacityVerifier::verify()`, the `OutputsSumOverflow` check is gated on:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(...OutputsSumOverflow...);
    }
}
``` [1](#0-0) 

The gate function is:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

The use of `.any()` means the entire capacity check is bypassed as soon as **one** input carries the DAO type script, regardless of how many non-DAO inputs are also present.

The code comment justifies this bypass with: *"DAO withdraw transaction is verified via the type script of DAO cells."* [3](#0-2) 

Per CKB RFC 0023, the DAO type script for phase-2 withdrawal checks **per-cell**: for each DAO input at index `i`, the output at index `i` must equal the maximum withdrawal amount. It does not verify the total transaction balance across all inputs and outputs. Non-DAO inputs and their corresponding outputs are invisible to the DAO type script.

The `DaoCalculator::transaction_maximum_withdraw` correctly accounts for all inputs (DAO and non-DAO) when computing fees: [4](#0-3) 

But this function is used only for fee calculation in the block assembler, not inside `CapacityVerifier`. The verifier either checks total balance (non-DAO path) or skips entirely (DAO path), with no middle path for mixed transactions.

### Impact Explanation

An attacker who holds a DAO cell in the prepare phase can construct a withdrawal transaction that also spends regular (non-DAO) cells. By setting the non-DAO output capacity higher than the non-DAO input capacity, the attacker inflates the total output beyond total input. The DAO type script passes (DAO output equals DAO max withdrawal), and `CapacityVerifier` skips the total balance check. The result is CKB created from nothing, violating the hard supply cap and breaking consensus-level accounting.

Concrete example:
- Input 0: DAO cell (100 CKB, max withdrawal 110 CKB)
- Input 1: Regular cell (50 CKB)
- Output 0: Regular cell (110 CKB) — correct DAO withdrawal, passes DAO type script
- Output 1: Regular cell (60 CKB) — 10 CKB more than input, **not checked by anyone**
- Net: 160 CKB in, 170 CKB out → 10 CKB inflated

### Likelihood Explanation

The attacker must own a DAO cell that has completed the two-phase deposit/prepare cycle (requiring at least one epoch lock period). This is a normal user action with no privileged access required. Any unprivileged transaction sender who has previously deposited into Nervos DAO can trigger this. The transaction is submitted via the standard `send_transaction` RPC, making the entry path fully reachable.

### Recommendation

Replace the `.any()` predicate in `valid_dao_withdraw_transaction` with a check that all inputs are DAO cells, or — preferably — perform a partial capacity check: verify that the sum of non-DAO inputs' capacity is greater than or equal to the sum of non-DAO outputs' capacity, while delegating only the DAO portion to the type script. Alternatively, use `DaoCalculator::transaction_fee` (which already correctly handles mixed inputs) as the authoritative capacity check for all DAO-containing transactions.

### Proof of Concept

1. Deposit 100 CKB into Nervos DAO → wait for interest to accrue → execute phase-1 prepare transaction.
2. Construct a phase-2 withdrawal transaction:
   - `inputs[0]`: the DAO prepare cell (100 CKB, max withdrawal = 110 CKB)
   - `inputs[1]`: any regular cell (50 CKB)
   - `outputs[0]`: regular cell with capacity = 110 CKB (satisfies DAO type script per-cell check)
   - `outputs[1]`: regular cell with capacity = 60 CKB (10 CKB more than `inputs[1]`)
3. Submit via `send_transaction` RPC.
4. `CapacityVerifier::valid_dao_withdraw_transaction()` returns `true` (input 0 uses DAO type script) → `OutputsSumOverflow` check is skipped entirely.
5. DAO type script verifies `outputs[0].capacity (110) == max_withdrawal(inputs[0]) (110)` → passes.
6. Transaction is accepted. Total inputs: 150 CKB. Total outputs: 170 CKB. 20 CKB created from nothing. [2](#0-1) [1](#0-0)

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

**File:** verification/src/transaction_verifier.rs (L517-522)
```rust
    fn valid_dao_withdraw_transaction(&self) -> bool {
        self.resolved_transaction
            .resolved_inputs
            .iter()
            .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
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
