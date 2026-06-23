### Title
Mixed DAO + Non-DAO Inputs Bypass Capacity Balance Check, Enabling CKB Inflation - (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for the **entire transaction** whenever **any** input cell uses the DAO type script. The DAO type script (running in CKB-VM) only validates the DAO-specific cells' withdrawal amounts; it does not enforce the capacity balance for non-DAO inputs in the same transaction. An attacker can mix one DAO input with arbitrary non-DAO inputs and set non-DAO outputs to exceed non-DAO inputs, creating CKB out of thin air.

---

### Finding Description

In `CapacityVerifier::verify()`, the `OutputsSumOverflow` guard is gated on a single boolean:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(...)
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

The inline comment justifies the skip with: *"DAO withdraw transaction is verified via the type script of DAO cells."* [3](#0-2) 

The DAO type script (RFC-0023) only verifies that each DAO output's capacity equals the correct interest-bearing withdrawal amount for the corresponding DAO input. It does **not** enforce a global `outputs_sum ≤ inputs_sum` constraint over the non-DAO cells in the same transaction. No other verifier in the pipeline fills this gap.

---

### Impact Explanation

**Impact: High**

An attacker who controls a DAO deposit cell (even a minimal one) can construct a withdrawal transaction that also consumes arbitrary non-DAO inputs and produces non-DAO outputs whose total capacity exceeds the non-DAO inputs' total capacity. The difference is unaccounted-for CKB created from nothing. Repeated exploitation inflates the native token supply, breaking the economic model of the chain and devaluing all existing CKB holdings.

---

### Likelihood Explanation

**Likelihood: High**

Any unprivileged transaction sender can craft such a transaction. No special role, key, or majority hashpower is required. The attacker only needs:
1. A valid DAO deposit cell (publicly creatable by anyone).
2. Any non-DAO live cell to use as an additional input.

The transaction is structurally valid and passes all other verifiers (script execution, since, etc.) because the DAO type script approves the DAO portion and no verifier checks the non-DAO balance.

---

### Recommendation

Replace the coarse-grained skip with a precise check that accounts for the legitimate DAO interest surplus. Specifically:

1. Compute `dao_interest` = sum of `(withdrawal_amount - deposit_capacity)` for each DAO input (using `DaoCalculator::transaction_maximum_withdraw` minus raw input capacity).
2. Enforce `outputs_sum ≤ inputs_sum + dao_interest` for every transaction, including those with DAO inputs.

This mirrors the correct fix suggested in the external report: handle each asset type correctly rather than bypassing the check entirely. [4](#0-3) 

---

### Proof of Concept

```
Setup:
  - Alice deposits 100 CKB into NervosDAO → dao_cell (100 CKB)
  - Alice also holds plain_cell (50 CKB, no type script)

After sufficient epochs, Alice constructs a withdrawal transaction:
  Inputs:
    [0] dao_cell        (100 CKB, DAO type script)  ← triggers valid_dao_withdraw_transaction()
    [1] plain_cell      (50 CKB,  no type script)

  Outputs:
    [0] dao_output      (110 CKB) ← DAO type script verifies this is correct with interest
    [1] plain_output    (60 CKB)  ← 10 CKB more than plain_cell input

  Total outputs = 170 CKB
  Total inputs  = 150 CKB
  Surplus       = 20 CKB (10 CKB DAO interest + 10 CKB fabricated from non-DAO inputs)

CapacityVerifier skips OutputsSumOverflow because valid_dao_withdraw_transaction() == true.
DAO type script approves [0] because 110 CKB == correct withdrawal amount.
No verifier checks that plain_output (60) > plain_cell (50).
Result: 10 CKB created from nothing, accepted as a valid block transaction.
```

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
