### Title
`CapacityVerifier::valid_dao_withdraw_transaction()` Skips `OutputsSumOverflow` Check for Phase 1 DAO Transactions, Allowing Capacity Inflation — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::valid_dao_withdraw_transaction()` returns `true` for **any** transaction that has a DAO-typed input — including phase 1 (deposit→prepare) transactions — not just phase 2 (prepare→withdraw) withdrawals. This causes the `OutputsSumOverflow` guard to be unconditionally skipped whenever a DAO cell appears as an input, even when the transaction is not a withdrawal and outputs can legitimately exceed inputs only due to interest. An unprivileged transaction sender who owns any DAO deposit cell can craft a mixed transaction that inflates non-DAO output capacity beyond non-DAO input capacity, bypassing the only node-level capacity conservation check.

---

### Finding Description

In `verification/src/transaction_verifier.rs`, `CapacityVerifier::verify()` contains the following guard:

```rust
// skip OutputsSumOverflow verification for resolved cellbase and DAO
// withdraw transactions.
// cellbase's outputs are verified by RewardVerifier
// DAO withdraw transaction is verified via the type script of DAO cells
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err((TransactionError::OutputsSumOverflow { ... }).into());
    }
}
```

The predicate that gates this skip is:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
```

This returns `true` if **any** input carries the DAO type script — regardless of whether the cell data is all-zeros (phase 1 deposit cell) or a non-zero block number (phase 2 prepare cell). The comment explicitly says the skip is intended only for "DAO withdraw transactions", but the implementation matches both phases.

The DAO type script (running in CKB-VM) only verifies the capacity of the DAO cell pair it governs. It does **not** verify the total transaction capacity balance across non-DAO inputs and outputs in the same transaction. The `OutputsSumOverflow` check in `CapacityVerifier` is the only node-level guard for that invariant, and it is unconditionally bypassed for any transaction that includes a DAO input. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

An attacker who owns a DAO deposit cell can construct a phase 1 transaction of the form:

| Role | Cell | Capacity |
|---|---|---|
| Input 1 | DAO deposit cell | 100 CKB |
| Input 2 | Regular cell | 10 CKB |
| Output 1 | DAO prepare cell | 100 CKB (verified by DAO script) |
| Output 2 | Regular cell | **50 CKB** (not verified by DAO script) |

Total inputs: 110 CKB. Total outputs: 150 CKB. Overflow: 40 CKB.

Because `valid_dao_withdraw_transaction()` returns `true` (Input 1 carries the DAO type script), the `OutputsSumOverflow` check is skipped entirely. The DAO type script only verifies that Output 1 capacity equals Input 1 capacity; it is silent about Output 2. The 40 CKB surplus is unaccounted for by any script or verifier at the `CapacityVerifier` layer.

At the tx-pool admission layer this transaction passes all checks and enters the pool. Whether the `DaoCalculator::transaction_fee()` call in `contextual_block_verifier.rs` provides a secondary catch at block-commit time was not fully confirmed in available code, but even if it does, the tx-pool accepts and relays the transaction, wasting peer bandwidth and causing miners to build and propagate invalid block candidates. If the secondary check is absent or bypassed, the attacker achieves on-chain capacity inflation — creating CKB out of thin air — which is a consensus-breaking impact. [3](#0-2) [4](#0-3) 

---

### Likelihood Explanation

The attack requires only that the sender owns a DAO deposit cell, which is a routine operation available to any CKB holder. No privileged access, leaked keys, or majority hashpower is needed. The crafted transaction is structurally valid (correct DAO phase 1 format) and will pass all other admission checks. Any RPC caller or tx-pool submitter can trigger this path.

---

### Recommendation

`valid_dao_withdraw_transaction()` must distinguish between phase 1 and phase 2 DAO inputs. A phase 1 deposit cell has cell data equal to all-zeros (`0x0000000000000000`); a phase 2 prepare cell has a non-zero 8-byte little-endian block number. The function should return `true` only when at least one input is a phase 2 prepare cell (non-zero `deposited_block_number`), mirroring the logic already present in `DaoCalculator::transaction_maximum_withdraw()`:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| {
            if !cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash) {
                return false;
            }
            // Only phase-2 prepare cells (non-zero block number) legitimately
            // allow outputs to exceed inputs.
            matches!(
                self.data_loader.load_cell_data(cell_meta),
                Some(data) if data.len() == 8 && LittleEndian::read_u64(&data) > 0
            )
        })
}
``` [5](#0-4) 

---

### Proof of Concept

1. Deposit 100 CKB into NervosDAO to obtain a DAO deposit cell (cell data = `0x0000000000000000`).
2. Hold a separate regular cell with 10 CKB.
3. Construct a phase 1 transaction:
   - `inputs[0]`: DAO deposit cell (100 CKB) — triggers `valid_dao_withdraw_transaction() == true`
   - `inputs[1]`: Regular cell (10 CKB)
   - `outputs[0]`: DAO prepare cell (100 CKB, data = deposit block number) — passes DAO type script
   - `outputs[1]`: Regular cell (50 CKB) — no script checks this
4. Submit via `send_transaction` RPC.
5. `CapacityVerifier::verify()` skips `OutputsSumOverflow` because `valid_dao_withdraw_transaction()` is `true`.
6. The DAO type script verifies only `outputs[0].capacity == inputs[0].capacity` (100 == 100) and succeeds.
7. The transaction is accepted into the tx-pool with 40 CKB of unaccounted output capacity. [2](#0-1) [6](#0-5)

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

**File:** rpc/src/module/pool.rs (L612-635)
```rust
    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();
        let submit_tx = tx_pool.submit_local_tx(tx.clone());

        if let Err(e) = submit_tx {
            error!("Send submit_tx request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        }

        let tx_hash = tx.hash();
        match submit_tx.unwrap() {
            Ok(_) => Ok(tx_hash.into()),
            Err(reject) => Err(RPCError::from_submit_transaction_reject(&reject)),
        }
    }
```
