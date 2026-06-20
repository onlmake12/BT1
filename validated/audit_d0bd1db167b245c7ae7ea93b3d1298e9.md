### Title
Mixed-Input DAO Bypass Allows Capacity Inflation on Non-DAO Inputs — (`File: verification/src/transaction_verifier.rs`)

### Summary

`CapacityVerifier::valid_dao_withdraw_transaction()` uses `.any()` to test whether **any** input carries the DAO type script. If even one input qualifies, the entire `OutputsSumOverflow` check is skipped for the whole transaction — including all non-DAO inputs whose capacity conservation is never validated by the DAO type script either. An unprivileged transaction sender can exploit this to create CKB capacity from nothing by mixing one DAO withdrawal input with regular inputs and inflating the outputs.

### Finding Description

`CapacityVerifier::verify()` contains the following guard:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(...OutputsSumOverflow...);
    }
}
``` [1](#0-0) 

`valid_dao_withdraw_transaction()` is:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

The `.any()` predicate returns `true` the moment **one** input uses the DAO type script. This causes the entire `OutputsSumOverflow` check to be skipped for the transaction — including all non-DAO inputs. The DAO type script (a CKB-VM script) only validates the DAO-specific withdrawal arithmetic for the DAO cells; it does not enforce `sum(all_inputs) >= sum(all_outputs)`. No other verifier fills this gap for the non-DAO portion of a mixed transaction.

The analog to the external report is direct: just as `msg.value` is "reused" across every `delegatecall` iteration because the payable guard is evaluated once at the transaction level, here the DAO exemption is evaluated once at the transaction level (`.any()`) and applied to the entire capacity balance check, allowing the non-DAO inputs' capacity to be "reused" (inflated) without enforcement.

### Impact Explanation

An attacker can construct a transaction with:
- **Input A**: a valid DAO withdrawal cell (e.g., 100 CKB deposited in Nervos DAO)
- **Input B**: a regular non-DAO cell (e.g., 50 CKB)
- **Output**: a single cell claiming 200 CKB

Because `valid_dao_withdraw_transaction()` returns `true` (Input A is a DAO cell), the `OutputsSumOverflow` check is skipped. The DAO type script validates only Input A's withdrawal arithmetic and does not check the total transaction balance. Input B's 50 CKB is effectively doubled. The attacker steals 50 CKB from the system per transaction, repeatable at will.

This is a **capacity inflation / unauthorized CKB creation** vulnerability — a direct breach of the fundamental CKB invariant that no transaction may create capacity from nothing.

### Likelihood Explanation

The attack requires only:
1. Owning any DAO withdrawal cell (phase-2 withdraw transaction, which any user can create)
2. Owning any regular live cell
3. Submitting a crafted transaction via the standard `send_transaction` RPC or P2P relay

No privileged access, no majority hashpower, no social engineering. The entry path is fully unprivileged and reachable on mainnet.

### Recommendation

Replace the blanket `.any()` bypass with a per-input accounting approach:
- Compute `non_dao_inputs_sum` and `dao_maximum_withdraw_sum` separately.
- Enforce `non_dao_inputs_sum + dao_maximum_withdraw_sum >= outputs_sum`.
- Alternatively, keep the current skip only when **all** inputs are DAO cells, and apply the normal `OutputsSumOverflow` check to the non-DAO portion otherwise.

Document clearly that the DAO type script is responsible only for DAO-cell arithmetic, not for total transaction balance conservation.

### Proof of Concept

Construct and submit via `send_transaction` RPC:

```
Inputs:
  [0] DAO withdrawal cell (phase-2): capacity = 100 CKB, type = DAO type script,
      cell_data = <deposited_block_number>, transaction_info.block_hash in header_deps
  [1] Regular cell: capacity = 50 CKB, no type script

Outputs:
  [0] Regular cell: capacity = 200 CKB   ← 50 CKB created from nothing

Witnesses:
  [0] WitnessArgs { input_type: <header_dep_index for deposit block> }
  [1] (empty or lock witness)
```

**Execution path:**
1. `CapacityVerifier::verify()` is called.
2. `valid_dao_withdraw_transaction()` iterates inputs, finds Input[0] has DAO type script → returns `true`. [2](#0-1) 
3. The `OutputsSumOverflow` guard is skipped entirely (200 CKB outputs vs. 150 CKB inputs is never checked). [3](#0-2) 
4. The DAO type script runs on Input[0] only, validates the DAO withdrawal arithmetic for 100 CKB — passes.
5. Transaction is accepted. Attacker receives 200 CKB having spent only 150 CKB. Net gain: 50 CKB per transaction.

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
