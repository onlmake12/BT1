### Title
Whole-Transaction Capacity Overflow Check Bypassed by Any DAO Input — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for the **entire transaction** whenever any single input cell carries the DAO type script. The DAO type script itself only verifies the capacity of DAO cells; it does not constrain non-DAO outputs. An attacker who controls a DAO cell can therefore include arbitrary non-DAO outputs whose total capacity exceeds the non-DAO inputs, minting CKB out of thin air.

---

### Finding Description

**Root cause — `CapacityVerifier::verify()`**

```rust
// verification/src/transaction_verifier.rs  lines 478-494
pub fn verify(&self) -> Result<(), Error> {
    // skip OutputsSumOverflow verification for resolved cellbase and DAO
    // withdraw transactions.
    // cellbase's outputs are verified by RewardVerifier
    // DAO withdraw transaction is verified via the type script of DAO cells
    if !(self.resolved_transaction.is_cellbase()
        || self.valid_dao_withdraw_transaction())   // ← OR branch
    {
        let inputs_sum  = self.resolved_transaction.inputs_capacity()?;
        let outputs_sum = self.resolved_transaction.outputs_capacity()?;
        if inputs_sum < outputs_sum {
            return Err(TransactionError::OutputsSumOverflow { … }.into());
        }
    }
    …
}
```

The helper that triggers the skip:

```rust
// lines 517-522
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta|
            cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
```

`valid_dao_withdraw_transaction()` returns `true` if **any** input carries the DAO type script. When it does, the entire `inputs_sum >= outputs_sum` guard is skipped for the whole transaction — including all non-DAO inputs and outputs.

The code comment claims "DAO withdraw transaction is verified via the type script of DAO cells." That is only partially true. The DAO type script (`util/dao/src/lib.rs`, `transaction_maximum_withdraw`) computes the maximum withdrawal for DAO cells and verifies the DAO-specific capacity. It does **not** verify that non-DAO outputs stay within non-DAO inputs. No other code path fills that gap once `CapacityVerifier` skips the check.

**Structural analogy to the Aragon bug:** In Aragon, a permission set on `address(this)` (self) caused the `_auth` OR-check to pass for *any* `_where` target. Here, a DAO input (the "self" equivalent) causes the capacity OR-check to pass for *any* non-DAO output, bypassing the intended constraint on the rest of the transaction.

**Exploit path:**

1. Attacker deposits CKB into Nervos DAO (standard operation, no privilege required).
2. After the lock period, attacker constructs a Phase-2 withdrawal transaction:
   - **Input 0**: DAO cell (e.g., 100 CKB) — triggers `valid_dao_withdraw_transaction() == true`
   - **Input 1**: regular cell (e.g., 1 CKB)
   - **Output 0**: DAO withdrawal output (e.g., 101 CKB with interest) — passes DAO type script
   - **Output 1**: regular output (e.g., 10 000 CKB) — **no check enforces this ≤ 1 CKB**
3. `CapacityVerifier::verify()` skips the overflow check because Input 0 is a DAO cell.
4. The DAO type script runs and approves the DAO cells only.
5. The transaction is accepted; the attacker receives 10 000 CKB from a 1 CKB regular input.

---

### Impact Explanation

**Unauthorized CKB issuance / theft of funds.** Any holder of a DAO cell can inflate their non-DAO outputs arbitrarily in a single withdrawal transaction. The excess capacity is drawn from the UTXO set of other users (the node accepts the transaction, spending live cells whose capacity is not covered by inputs). This directly violates CKB's core monetary invariant (`sum(outputs) ≤ sum(inputs)` for non-cellbase transactions) and enables unlimited, unprivileged theft of on-chain value.

Impact score: **Critical** (direct, unlimited asset theft by any DAO depositor).

---

### Likelihood Explanation

Any CKB holder who has ever deposited into Nervos DAO — a standard, widely-used feature — can trigger this after their lock period expires. No special keys, no operator access, no majority hashpower. The attacker-controlled entry point is the standard `send_transaction` RPC / P2P relay path. Likelihood: **High** (low barrier, large existing population of DAO depositors).

---

### Recommendation

Replace the whole-transaction skip with a per-cell capacity split: compute `non_dao_inputs_sum` and `non_dao_outputs_sum` separately, and enforce `non_dao_inputs_sum >= non_dao_outputs_sum` even when DAO inputs are present. The DAO type script continues to govern the DAO-cell portion; the node-level check governs the rest.

```rust
pub fn verify(&self) -> Result<(), Error> {
    if !self.resolved_transaction.is_cellbase() {
        let (dao_inputs, regular_inputs): (Vec<_>, Vec<_>) = self
            .resolved_transaction
            .resolved_inputs
            .iter()
            .partition(|m| cell_uses_dao_type_script(&m.cell_output, &self.dao_type_hash));

        if dao_inputs.is_empty() {
            // No DAO inputs: full check as before
            let inputs_sum  = self.resolved_transaction.inputs_capacity()?;
            let outputs_sum = self.resolved_transaction.outputs_capacity()?;
            if inputs_sum < outputs_sum {
                return Err(TransactionError::OutputsSumOverflow { inputs_sum, outputs_sum }.into());
            }
        } else {
            // DAO inputs present: only skip the DAO-cell portion
            let non_dao_inputs_sum: Capacity = regular_inputs.iter()
                .try_fold(Capacity::zero(), |acc, m| acc.safe_add(m.cell_output.capacity().into()))?;
            // non-DAO outputs must not exceed non-DAO inputs
            // (DAO outputs are verified by the DAO type script)
            let non_dao_outputs_sum = /* sum outputs without DAO type script */;
            if non_dao_inputs_sum < non_dao_outputs_sum {
                return Err(TransactionError::OutputsSumOverflow { … }.into());
            }
        }
    }
    // per-output occupied-capacity check unchanged …
}
```

---

### Proof of Concept

```
Transaction structure:
  Inputs:
    [0] DAO cell:     capacity = 100 CKB, type = DAO type script
                      (data = 8-byte block number → withdrawal phase 2)
    [1] Regular cell: capacity = 1 CKB,   type = None

  Outputs:
    [0] Regular cell: capacity = 101 CKB  (DAO withdrawal, passes DAO type script)
    [1] Regular cell: capacity = 9 999 CKB (attacker's gain, NO check enforced)

  header_deps: [deposit_block_hash, withdraw_block_hash]
  witnesses:   [WitnessArgs { input_type: deposit_header_index }, ...]

Verification path:
  CapacityVerifier::verify()
    → valid_dao_withdraw_transaction() == true  (Input[0] has DAO type script)
    → OutputsSumOverflow check SKIPPED entirely
    → per-output occupied-capacity check passes (each output ≥ its own occupied capacity)
  ScriptVerifier::verify()
    → DAO type script runs for Input[0]/Output[0]: 101 CKB == 100 CKB + interest ✓
    → No script governs Output[1]
  Transaction ACCEPTED.

Net effect: attacker spent 1 CKB regular input, received 9 999 CKB regular output.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
