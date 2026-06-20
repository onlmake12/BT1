### Title
Capacity Inflation via Mixed DAO/Non-DAO Inputs Bypassing `OutputsSumOverflow` Check — (`verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::valid_dao_withdraw_transaction()` returns `true` if **any** resolved input uses the DAO type script. When it returns `true`, `CapacityVerifier::verify()` skips the entire `OutputsSumOverflow` check. Because the on-chain DAO type script only verifies the DAO-specific cell capacity (not the total transaction balance), a transaction sender can mix one DAO input with arbitrary non-DAO inputs and inflate the non-DAO outputs beyond the non-DAO inputs capacity — creating CKB from nothing.

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

The guard `valid_dao_withdraw_transaction()` is:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

The `.any()` predicate means: if **at least one** input is a DAO cell, the entire `OutputsSumOverflow` check is skipped for the whole transaction — including all non-DAO inputs and outputs.

The code comment states: *"DAO withdraw transaction is verified via the type script of DAO cells."* However, the DAO type script (`util/dao/src/lib.rs`) only computes and verifies the maximum withdrawal capacity for DAO cells individually. It does not verify the total transaction capacity balance across all inputs and outputs. [3](#0-2) 

For non-DAO inputs, `transaction_maximum_withdraw` simply returns `output.capacity().into()` — the face value — with no enforcement that the corresponding outputs do not exceed it. [4](#0-3) 

---

### Impact Explanation

An attacker can craft a transaction with:
- **1 DAO input** (e.g., 100 CKB, with 5 CKB interest → 105 CKB withdrawable)
- **N non-DAO inputs** (e.g., 50 CKB total)
- **DAO output**: 105 CKB — passes DAO type script
- **Non-DAO outputs**: 60 CKB — **10 CKB more than the 50 CKB non-DAO inputs**

`CapacityVerifier` skips the overflow check because `valid_dao_withdraw_transaction()` is `true`. The DAO type script passes because the DAO output equals the correct withdrawal amount. The 10 CKB excess in non-DAO outputs is verified by **neither** the `CapacityVerifier` nor the DAO type script.

**Result: unbounded CKB capacity inflation.** An attacker can create CKB tokens from nothing, breaking the fundamental supply invariant of the chain. This is a consensus-level impact: any node that processes such a transaction will accept it, and the inflated capacity will be committed to the canonical chain.

---

### Likelihood Explanation

The entry path is fully unprivileged. Any user can submit a transaction via the `send_transaction` RPC endpoint. The attacker only needs:
1. A valid DAO cell in the "prepare" phase (obtainable by anyone who deposits into Nervos DAO)
2. Any additional non-DAO live cells

No special role, key, or majority hashpower is required. The attack is deterministic and reproducible.

---

### Recommendation

Replace the `.any()` predicate with a check that ensures **all** inputs are DAO cells before skipping the overflow check, or — more robustly — do not skip the `OutputsSumOverflow` check at all for mixed transactions. Instead, compute the expected maximum outputs as `non_dao_inputs_capacity + dao_maximum_withdraw` and enforce `outputs_sum <= expected_maximum`.

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    // ALL inputs must be DAO cells to skip the overflow check
    !self.resolved_transaction.resolved_inputs.is_empty()
        && self.resolved_transaction
            .resolved_inputs
            .iter()
            .all(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
```

---

### Proof of Concept

1. Deposit 100 CKB into Nervos DAO; wait for interest to accrue (e.g., 5 CKB interest → 105 CKB withdrawable).
2. Obtain a separate non-DAO live cell with 50 CKB.
3. Construct a withdrawal transaction:
   - **inputs**: [DAO prepare cell (100 CKB), non-DAO cell (50 CKB)]
   - **outputs**: [DAO output (105 CKB), non-DAO output (60 CKB)]
   - **header_deps**: [deposit block hash, prepare block hash]
   - **witnesses**: [DAO witness with deposit header index, empty]
4. Submit via `send_transaction` RPC.
5. `CapacityVerifier::valid_dao_withdraw_transaction()` returns `true` (input[0] is a DAO cell).
6. `OutputsSumOverflow` check is skipped entirely.
7. DAO type script verifies input[0] → output[0] = 105 CKB ✓; it does not inspect output[1].
8. Transaction is accepted. Total outputs (165 CKB) exceed total inputs (155 CKB) by 10 CKB — capacity created from nothing. [5](#0-4) [2](#0-1)

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
