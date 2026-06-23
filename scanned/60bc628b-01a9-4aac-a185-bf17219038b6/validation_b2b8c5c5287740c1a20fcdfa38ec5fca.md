### Title
`CapacityVerifier::valid_dao_withdraw_transaction()` Overly Broad Check Skips Entire `OutputsSumOverflow` Verification for Mixed DAO/Non-DAO Transactions, Enabling Non-DAO Capacity Inflation — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for any transaction that contains **any** DAO-type-script input. Because the DAO type script only verifies the output cell at the same index as the DAO input, non-DAO outputs in the same transaction are verified by neither the DAO type script nor the `CapacityVerifier`. An attacker who holds a DAO withdrawing cell can craft a transaction that inflates non-DAO outputs beyond the non-DAO inputs, creating CKB capacity from nothing.

---

### Finding Description

In `CapacityVerifier::verify()`, the `OutputsSumOverflow` guard is gated on a single boolean:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum  = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(TransactionError::OutputsSumOverflow { … }.into());
    }
}
``` [1](#0-0) 

`valid_dao_withdraw_transaction()` returns `true` if **any** resolved input carries the DAO type script:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

The code comment explains the intent: *"DAO withdraw transaction is verified via the type script of DAO cells."* However, the DAO type script (RFC-0023) only verifies the **output at the same positional index** as the DAO input — it checks that `output[i].capacity >= calculated_maximum_withdraw`. It does **not** verify the total transaction balance, and it does not inspect non-DAO outputs at other indices.

This is structurally identical to the reported Solidity bug: the check (`isNativeAsset` / "any input uses DAO type script") validates a type/state property rather than the actual origin or amounts, leaving a gap that allows unintended value to flow through unchecked.

The `DaoCalculator::transaction_maximum_withdraw` confirms the DAO type script's scope: it only computes the maximum withdraw for cells that carry the DAO type script; all other inputs simply contribute their face-value capacity. [3](#0-2) 

---

### Impact Explanation

A transaction with:
- `Input[0]`: DAO withdrawing cell (e.g., 100 CKB)
- `Input[1]`: Regular cell (e.g., 10 CKB)
- `Output[0]`: Regular cell (105 CKB — correct DAO withdrawal, verified by DAO type script)
- `Output[1]`: Regular cell (20 CKB — **10 CKB more than Input[1]**, verified by nothing)

passes all checks:
1. DAO type script verifies `Output[0].capacity == 105 CKB` ✓
2. `CapacityVerifier` skips the sum check because `Input[0]` uses DAO type script ✓
3. `Output[1]` is unchecked by both ✓

Result: **10 CKB created from nothing** — a direct violation of CKB's capacity conservation invariant. Repeated exploitation inflates the total CKB supply, undermining the economic security of the chain.

---

### Likelihood Explanation

**Medium.** The attacker must:
1. Own a DAO deposit cell (requires a prior on-chain deposit).
2. Wait through the DAO lock period (~180 epochs, roughly 30 days on mainnet) to reach phase-1 withdrawal.
3. Craft a phase-2 withdrawal transaction with inflated non-DAO outputs.

No privileged role, leaked key, or majority hashpower is required. Any unprivileged transaction sender who has previously deposited into the DAO can execute this. The attack is repeatable and the inflation compounds with each withdrawal cycle.

---

### Recommendation

Replace the all-or-nothing bypass with a **split capacity check**:

1. Compute `dao_input_capacity` = sum of capacities of DAO-type-script inputs.
2. Compute `dao_max_withdraw` = `DaoCalculator::transaction_maximum_withdraw(rtx)`.
3. Compute `non_dao_input_capacity` = `total_inputs_capacity - dao_input_capacity`.
4. Compute `non_dao_output_capacity` = `total_outputs_capacity - dao_output_capacity` (where `dao_output_capacity` is the capacity of outputs at the same indices as DAO inputs).
5. Assert `non_dao_output_capacity <= non_dao_input_capacity` (the non-DAO portion must still be balanced).
6. Let the DAO type script continue to enforce the DAO-specific output amount.

Alternatively, the check in `valid_dao_withdraw_transaction` should be narrowed to only suppress the overflow check for the DAO-indexed output, not the entire transaction sum.

---

### Proof of Concept

```
// Setup: attacker has completed DAO phase-1 withdrawal
// dao_withdrawing_cell: 100 CKB, uses DAO type script, data = deposit_block_number (> 0)
// regular_cell: 10 CKB, uses attacker's lock script

Transaction {
    inputs: [
        CellInput { out_point: dao_withdrawing_cell, since: <unlock epoch> },  // index 0
        CellInput { out_point: regular_cell, since: 0 },                       // index 1
    ],
    outputs: [
        CellOutput { capacity: 105 CKB, lock: attacker_lock, type: None },     // index 0 — DAO type script verifies this
        CellOutput { capacity: 20 CKB,  lock: attacker_lock, type: None },     // index 1 — UNCHECKED
    ],
    witnesses: [
        WitnessArgs { input_type: <deposit_header_dep_index as u64 LE> },      // for DAO input
        WitnessArgs { … },                                                      // for regular input
    ],
    header_deps: [deposit_block_hash, withdrawing_block_hash],
    cell_deps:   [dao_cell_dep, attacker_lock_cell_dep],
}

// CapacityVerifier: valid_dao_withdraw_transaction() == true → sum check SKIPPED
// DAO type script: Output[0].capacity (105) >= max_withdraw (105) → PASS
// Output[1].capacity (20) vs Input[1].capacity (10): NO CHECK EXISTS
// Net result: 10 CKB inflated into existence
```

The root cause is in `verification/src/transaction_verifier.rs` at `valid_dao_withdraw_transaction()` (lines 517–522) and its use at line 483, which mirrors the pattern of checking `isNativeAsset` instead of `msg.sender == address(asset)` — a type/state property check that is too coarse to prevent unintended value flows. [4](#0-3) [2](#0-1)

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
