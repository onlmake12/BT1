### Title
Mixed DAO/Non-DAO Transaction Bypasses Global Capacity Balance Invariant - (File: `verification/src/transaction_verifier.rs`)

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for the **entire** transaction whenever **any** input carries the DAO type script. The DAO type script (on-chain) only validates the capacity of the specific DAO cell, not the total transaction balance. A transaction sender can therefore include one DAO input alongside non-DAO inputs and inflate the capacity of non-DAO outputs beyond what the non-DAO inputs provide, creating CKB shannons from nothing.

### Finding Description

`CapacityVerifier::verify()` enforces two invariants:

1. `inputs_sum >= outputs_sum` (global balance)
2. Per-output: `output.capacity >= output.occupied_capacity`

The global balance check is gated behind a single condition:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(...)
    }
}
``` [1](#0-0) 

`valid_dao_withdraw_transaction()` returns `true` if **any** resolved input uses the DAO type script:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

The code comment states: *"DAO withdraw transaction is verified via the type script of DAO cells."* However, the DAO type script (the on-chain C script) only verifies the capacity of the specific DAO output at the same index as the DAO input — it does not verify the total transaction balance. Non-DAO outputs in the same transaction are verified by no one when the global check is skipped.

The per-output occupied-capacity check (lines 496–512) still runs, but it only enforces `output.capacity >= output.occupied_capacity` — it does not enforce that the output capacity is backed by inputs. [3](#0-2) 

### Impact Explanation

An attacker can construct a transaction:

| Slot | Role | Capacity |
|------|------|----------|
| Input 0 | DAO cell (type = DAO) | D |
| Input 1 | Normal cell | N |
| Output 0 | Normal cell (DAO withdrawal) | D + interest |
| Output 1 | Normal cell | N + X (X > 0, attacker-chosen) |

- `valid_dao_withdraw_transaction()` returns `true` → global balance check skipped.
- The DAO type script validates Output 0 (capacity = D + interest).
- Output 1 (capacity = N + X) is validated by no verifier.
- Total outputs exceed total inputs by X shannons.

This violates the fundamental CKB invariant that non-cellbase transactions cannot create capacity. The attacker inflates the CKB supply by an arbitrary amount X per transaction, bounded only by the minimum occupied capacity of Output 1.

The analog to OUSD is direct: just as `changeSupply` changes `_totalSupply` without adjusting opted-out balances, `CapacityVerifier` skips the global balance check without accounting for the non-DAO portion of the transaction, allowing the sum of outputs to exceed the sum of inputs.

### Likelihood Explanation

Any unprivileged transaction sender who has previously deposited into NervosDAO (a public, permissionless operation) can execute this attack. The attacker needs:
1. One DAO cell in the withdrawal phase (publicly creatable).
2. One normal cell (any live cell they own).
3. Craft a transaction with the structure above and submit via the `send_transaction` RPC.

No special privileges, leaked keys, or majority hashpower are required.

### Recommendation

Replace the `.any()` predicate with a check that enforces the global balance invariant for the non-DAO portion of the transaction. Specifically:

- Compute `dao_inputs_max_withdraw` (sum of maximum withdrawable capacity for DAO inputs) and `non_dao_inputs_capacity` separately.
- Compute `dao_outputs_capacity` and `non_dao_outputs_capacity` separately.
- Enforce: `non_dao_inputs_capacity >= non_dao_outputs_capacity` unconditionally.
- Enforce: `dao_inputs_max_withdraw + non_dao_inputs_capacity >= total_outputs_capacity` (or delegate the DAO portion entirely to the type script and enforce the non-DAO balance in `CapacityVerifier`).

### Proof of Concept

```
// Setup:
// - DAO cell: capacity = 1000 CKB, deposited at block B1
// - Normal cell: capacity = 100 CKB
// - Current block B2 (after lock period), AR ratio gives interest = 10 CKB

Transaction {
    inputs: [
        CellInput { out_point: dao_cell_outpoint, since: <unlock epoch> },  // DAO input
        CellInput { out_point: normal_cell_outpoint, since: 0 },            // Normal input
    ],
    outputs: [
        CellOutput { capacity: 1010 CKB, lock: attacker_lock, type: None }, // DAO withdrawal (validated by DAO script)
        CellOutput { capacity: 200 CKB, lock: attacker_lock, type: None },  // 100 CKB extra (UNCHECKED)
    ],
    witnesses: [ <valid DAO witness>, <normal witness> ],
    header_deps: [ deposit_block_hash, withdraw_block_hash ],
}

// Total inputs:  1000 + 100 = 1100 CKB
// Total outputs: 1010 + 200 = 1210 CKB
// Net created:   110 CKB (10 from DAO interest + 100 from unchecked non-DAO output)

// CapacityVerifier: skips global check (valid_dao_withdraw_transaction() == true)
// DAO type script: validates output[0] capacity == 1010 CKB ✓
// output[1] capacity: validated by nobody ✗
``` [4](#0-3) [5](#0-4)

### Citations

**File:** verification/src/transaction_verifier.rs (L461-523)
```rust
/// Perform inputs and outputs `capacity` field related verification
pub struct CapacityVerifier {
    resolved_transaction: Arc<ResolvedTransaction>,
    dao_type_hash: Byte32,
}

impl CapacityVerifier {
    /// Create a new `CapacityVerifier`
    pub fn new(resolved_transaction: Arc<ResolvedTransaction>, dao_type_hash: Byte32) -> Self {
        CapacityVerifier {
            resolved_transaction,
            dao_type_hash,
        }
    }

    /// Verify sum of inputs capacity should be greater than or equal to sum of outputs capacity
    /// Verify outputs capacity should be greater than or equal to its occupied capacity
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
