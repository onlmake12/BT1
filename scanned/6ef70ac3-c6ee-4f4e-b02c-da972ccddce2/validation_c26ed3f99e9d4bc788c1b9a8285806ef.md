### Title
Mixed-Input DAO Transaction Bypasses `OutputsSumOverflow` Capacity Check, Enabling Capacity Inflation — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for the **entire transaction** whenever any single input cell carries the DAO type script. Because the DAO on-chain type script only enforces the capacity constraint for the DAO cell itself, the capacity balance of all co-mingled non-DAO inputs is left completely unverified. An unprivileged transaction sender can exploit this to inflate outputs beyond the sum of non-DAO inputs, creating CKB capacity from nothing.

---

### Finding Description

In `CapacityVerifier::verify()`, the guard condition is:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(TransactionError::OutputsSumOverflow { ... }.into());
    }
}
``` [1](#0-0) 

`valid_dao_withdraw_transaction()` returns `true` if **any** input uses the DAO type script:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

The code comment explains the intent: "DAO withdraw transaction is verified via the type script of DAO cells." However, the DAO type script (the on-chain C script) only enforces that the **DAO cell's own output capacity** does not exceed its maximum withdrawal amount. It does not enforce the global transaction balance across all inputs and outputs. [3](#0-2) 

When a transaction mixes DAO inputs with regular non-DAO inputs, the `OutputsSumOverflow` check is skipped for the whole transaction. The DAO type script runs and validates only the DAO cell's portion. The non-DAO inputs' capacity is never balanced against the outputs.

---

### Impact Explanation

An attacker who owns a DAO cell (phase-2 withdrawal) and any regular cell can construct a transaction:

- **Input 0**: DAO cell — 1,000 CKB, `max_withdraw` = 1,010 CKB
- **Input 1**: Regular cell — 100 CKB
- **Output 0**: Regular cell — 1,010 CKB (passes DAO type script check)
- **Output 1**: Regular cell — 200 CKB (**100 CKB created from nothing**)

`CapacityVerifier` skips the sum check because `valid_dao_withdraw_transaction()` returns `true`. The DAO type script approves Output 0 (≤ max_withdraw). Output 1's 200 CKB is never checked against Input 1's 100 CKB. The attacker extracts 100 CKB of capacity that did not exist in the inputs.

This is a direct capacity inflation / asset creation vulnerability. Repeated exploitation can drain the total CKB supply invariant.

**Impact: 4 / 5** — direct on-chain capacity inflation, consensus-level violation.

---

### Likelihood Explanation

The attacker only needs:
1. A valid DAO withdrawal cell (phase 2), which any user can create by depositing into the DAO and waiting the lock period.
2. Any regular live cell to co-mingle as a non-DAO input.

No privileged access, no majority hashpower, no social engineering. The transaction is submitted via the standard RPC (`send_transaction`) or relayed via P2P. The tx-pool and block verifier both call `ContextualTransactionVerifier::verify()`, which calls `self.capacity.verify()`. [4](#0-3) 

**Likelihood: 3 / 5** — requires owning a DAO cell, but this is a normal user action with no special privileges.

---

### Recommendation

The `OutputsSumOverflow` check must not be skipped wholesale for the entire transaction. Instead, the check should be applied to the non-DAO inputs and outputs independently, or the guard should be tightened so that only the DAO cell's own capacity delta is exempted. One correct approach:

- Compute `non_dao_inputs_sum` (inputs without DAO type script) and `non_dao_outputs_sum` (outputs not attributed to DAO withdrawal), and enforce `non_dao_inputs_sum >= non_dao_outputs_sum` separately from the DAO cell's own capacity check.

Alternatively, the DAO type script itself should be upgraded to enforce the global transaction balance, so the node-level bypass is safe.

---

### Proof of Concept

Construct a `ResolvedTransaction` with:
- `resolved_inputs[0]`: a DAO cell (type script = DAO type hash, `hash_type = Type`), capacity = 1,000 CKB, `data` = 8-byte deposited block number > 0
- `resolved_inputs[1]`: a plain cell, capacity = 100 CKB
- `transaction.outputs[0]`: capacity = 1,010 CKB (≤ DAO max_withdraw, passes DAO script)
- `transaction.outputs[1]`: capacity = 200 CKB (100 CKB excess, never checked)

Pass this to `CapacityVerifier::new(rtx, dao_type_hash).verify()`.

`valid_dao_withdraw_transaction()` returns `true` (Input 0 has DAO type script). [2](#0-1) 

The `OutputsSumOverflow` block is skipped entirely. [5](#0-4) 

The per-output `InsufficientCellCapacity` loop only checks that each output's capacity ≥ its own occupied capacity — it does not check the global sum. [6](#0-5) 

`verify()` returns `Ok(())`. The 100 CKB excess in Output 1 is accepted by consensus, inflating the total capacity supply.

### Citations

**File:** verification/src/transaction_verifier.rs (L162-164)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
```

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

**File:** verification/src/transaction_verifier.rs (L496-512)
```rust
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
