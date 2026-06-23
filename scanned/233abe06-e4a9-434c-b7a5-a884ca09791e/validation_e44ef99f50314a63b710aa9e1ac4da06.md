### Title
DAO Deposit-Prepare Pair Matched by Index Position Instead of Cell Identity, Allowing Lock-Script-Size Check Bypass — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier::verify()` pairs transaction inputs with outputs using a positional `zip()` iterator. Because CKB places no protocol constraint that `input[i]` corresponds to `output[i]`, an attacker can misalign DAO deposit inputs and DAO prepare outputs so that neither pair satisfies the "both must be DAO" guard, silently skipping the lock-script-size enforcement. This is the direct CKB analog of the MarinateV2 token-ID confusion bug: ownership/identity is tracked by position rather than by the unique cell identifier (OutPoint).

---

### Finding Description

`DaoScriptSizeVerifier` was introduced as a node-level workaround because the on-chain DAO script cannot itself enforce that the lock script size is unchanged between the deposit cell (Phase 1 input) and the prepare cell (Phase 1 output). The verifier's `verify()` method iterates over pairs `(resolved_inputs[i], outputs[i])`:

```rust
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())
    .enumerate()
{
    if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
        && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
    {
        continue;                          // ← skips if either side is not DAO
    }
    ...
    if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
        return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
    }
}
``` [1](#0-0) 

The guard on line 854–858 requires **both** `input[i]` and `output[i]` to carry the DAO type script. An attacker constructs a Phase 1 transaction with deliberately misaligned positions:

| Index | Input | Output |
|-------|-------|--------|
| 0 | non-DAO cell | DAO prepare cell (small lock script) |
| 1 | DAO deposit cell (large lock script) | non-DAO cell |

`zip` produces two pairs:
- `(non-DAO input[0], DAO output[0])` → guard fails (input not DAO) → `continue`
- `(DAO input[1], non-DAO output[1])` → guard fails (output not DAO) → `continue`

The lock-script-size check is never reached. The DAO deposit cell with a large lock script is silently paired with a prepare cell that carries a smaller lock script.

The verifier's own comment acknowledges it is the sole enforcement layer for this rule: [2](#0-1) 

---

### Impact Explanation

In CKB's Nervos DAO, the interest paid in Phase 2 is computed on `counted_capacity = total_capacity − occupied_capacity`, where `occupied_capacity` includes the lock script size. If the prepare cell carries a **smaller** lock script than the deposit cell, `occupied_capacity` shrinks and `counted_capacity` grows — yielding more interest than the depositor is entitled to, at the expense of the shared DAO interest pool (funded by CKB secondary issuance). Other DAO participants receive proportionally less.

The `DaoCalculator::transaction_maximum_withdraw` function, which performs the interest arithmetic, operates on the prepare cell's actual capacity and lock script size without re-checking that they match the original deposit: [3](#0-2) 

---

### Likelihood Explanation

Any CKB address holder who has deposited into the DAO can exploit this. The attacker only needs to:
1. Deposit CKB with a large lock script.
2. Submit a Phase 1 (prepare) transaction with the DAO input at index ≥ 1 and the DAO output at index 0 (or any other misalignment), inserting a filler non-DAO input/output at index 0/1 respectively.
3. Proceed to Phase 2 withdrawal, collecting inflated interest.

No special privilege, key leak, or majority hashpower is required. The transaction is valid from the perspective of all other verifiers (capacity conservation, script execution, since rules).

---

### Recommendation

Replace the positional `zip` with an explicit identity-aware pairing. The verifier should match each DAO deposit input to its corresponding DAO prepare output by **cell identity** (e.g., by matching the type script args or by requiring the transaction to declare the pairing explicitly in witnesses), rather than assuming `input[i]` corresponds to `output[i]`. Until a proper fix is deployed, the verifier should at minimum reject any transaction where the count of DAO deposit inputs does not equal the count of DAO prepare outputs, and enforce a strict 1-to-1 positional layout (i.e., all DAO inputs must appear before all non-DAO inputs, mirrored in outputs).

---

### Proof of Concept

Construct a Phase 1 transaction:

```
inputs:
  [0] = any live non-DAO cell (e.g., plain CKB cell)
  [1] = DAO deposit cell, lock script = 100 bytes, capacity = C

outputs:
  [0] = DAO prepare cell, lock script = 20 bytes, capacity = C
  [1] = change cell (non-DAO)
```

`DaoScriptSizeVerifier` iterates:
- `i=0`: `(non-DAO input, DAO output)` → `cell_uses_dao_type_script(input)` is false → `continue`
- `i=1`: `(DAO input, non-DAO output)` → `cell_uses_dao_type_script(output)` is false → `continue`

Lock-script-size check is never executed. The transaction is accepted. In Phase 2, `DaoCalculator` computes interest on `counted_capacity = C − occupied_capacity(20-byte lock)`, which is larger than the legitimate `C − occupied_capacity(100-byte lock)`, yielding excess interest. [4](#0-3) [5](#0-4)

### Citations

**File:** verification/src/transaction_verifier.rs (L817-818)
```rust
/// Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts.
/// It provides a temporary solution till Nervos DAO script can be properly upgraded.
```

**File:** verification/src/transaction_verifier.rs (L845-890)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao_type_hash = self.dao_type_hash();
        for (i, (input_meta, cell_output)) in self
            .resolved_transaction
            .resolved_inputs
            .iter()
            .zip(self.resolved_transaction.transaction.outputs())
            .enumerate()
        {
            // Both the input and output cell must use Nervos DAO as type script
            if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
                && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
            {
                continue;
            }

            // A Nervos DAO deposit cell must have input data
            let input_data = match self.data_loader.load_cell_data(input_meta) {
                Some(data) => data,
                None => continue,
            };

            // Only input data with full zeros are counted as deposit cell
            if input_data.into_iter().any(|b| b != 0) {
                continue;
            }

            // Only cells committed after the pre-defined block number in consensus is
            // applied to this rule
            if let Some(info) = &input_meta.transaction_info
                && info.block_number
                    < self
                        .consensus
                        .starting_block_limiting_dao_withdrawing_lock()
            {
                continue;
            }

            // Now we have a pair of DAO deposit and withdrawing cells, it is expected
            // they have the lock scripts of the same size.
            if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
                return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
            }
        }
        Ok(())
    }
```

**File:** util/dao/src/lib.rs (L38-123)
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
```

**File:** util/dao/src/lib.rs (L148-157)
```rust

        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

```
