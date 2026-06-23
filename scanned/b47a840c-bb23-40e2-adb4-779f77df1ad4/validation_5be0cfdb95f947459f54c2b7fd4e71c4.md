### Title
DAO Withdraw Check Bypasses `OutputsSumOverflow` Validation for All Non-DAO Inputs — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` skips the `OutputsSumOverflow` check for the **entire transaction** whenever any single input cell carries the DAO type script. The DAO type script itself only enforces DAO-specific withdrawal rules and does not verify the global inputs-sum ≥ outputs-sum invariant. A transaction sender can therefore mix one DAO input with arbitrary non-DAO inputs and inflate non-DAO outputs beyond what the non-DAO inputs supply, minting CKB out of thin air.

---

### Finding Description

`CapacityVerifier::verify()` contains the following guard:

```rust
// verification/src/transaction_verifier.rs  lines 479-494
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum  = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(TransactionError::OutputsSumOverflow { ... }.into());
    }
}
```

The predicate that triggers the bypass is:

```rust
// lines 517-522
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
```

`any()` returns `true` the moment **one** input carries the DAO type script. When it does, the `OutputsSumOverflow` guard is skipped for **all** inputs and **all** outputs in the transaction — including every non-DAO cell.

The code comment reads: *"DAO withdraw transaction is verified via the type script of DAO cells."* The DAO type script (RFC-0023) verifies only:
- that the output data for a deposit cell is all-zeros, and
- that the withdrawal output capacity equals the deposit principal plus accrued interest.

It does **not** verify the global balance `Σ inputs ≥ Σ outputs`. No other consensus path enforces this invariant once `CapacityVerifier` skips it.

---

### Impact Explanation

An attacker who controls a DAO cell can craft a transaction where:

| Cell | Capacity |
|---|---|
| Input 1 — DAO cell (deposit) | 100 CKB |
| Input 2 — ordinary cell | 50 CKB |
| Output 1 — DAO withdrawal | 105 CKB (DAO script accepts: principal + interest) |
| Output 2 — ordinary cell | 100 CKB (**50 CKB fabricated**) |

`valid_dao_withdraw_transaction()` returns `true` because Input 1 carries the DAO type script. The `OutputsSumOverflow` check is skipped. The DAO type script runs on the DAO cells and accepts Output 1 as a valid withdrawal. No check ever compares the 50 CKB ordinary input against the 100 CKB ordinary output. The transaction is accepted and 50 CKB is minted from nothing.

**Impact: Critical** — direct inflation of the CKB token supply, undermining monetary policy and consensus integrity.

---

### Likelihood Explanation

**Likelihood: High.** Any unprivileged transaction sender who holds a DAO deposit cell (a common, publicly accessible operation) can construct this transaction. No special access, no majority hash power, no social engineering is required. The attacker controls the transaction structure entirely through the standard RPC (`send_transaction`) or P2P relay path.

---

### Recommendation

Replace the coarse `any()`-based predicate with a precise per-cell accounting approach:

1. Compute `dao_inputs_sum` and `dao_outputs_sum` for cells that carry the DAO type script.
2. Compute `non_dao_inputs_sum` and `non_dao_outputs_sum` for all remaining cells.
3. Enforce `non_dao_inputs_sum ≥ non_dao_outputs_sum` unconditionally; let the DAO type script enforce the DAO-cell balance as it already does.

Alternatively, remove the blanket skip and instead rely entirely on the DAO type script to enforce the DAO portion, while always running the global `OutputsSumOverflow` check. This requires confirming the DAO type script enforces the global invariant — which it currently does not.

---

### Proof of Concept

**Entry path:** unprivileged RPC caller via `send_transaction` or P2P relayer submitting a crafted transaction.

**Steps:**

1. Deposit 100 CKB into the DAO (standard operation, creates a DAO cell).
2. Wait for the DAO lock period to elapse.
3. Construct a withdrawal transaction:
   - **Input 0:** the DAO cell (100 CKB, DAO type script present).
   - **Input 1:** any ordinary live cell (50 CKB, no type script).
   - **Output 0:** DAO withdrawal cell, capacity = 105 CKB (principal + interest; DAO type script accepts this).
   - **Output 1:** ordinary cell, capacity = 100 CKB (50 CKB more than Input 1 supplies).
4. Submit the transaction.
5. `CapacityVerifier::verify()` calls `valid_dao_withdraw_transaction()`, which returns `true` because Input 0 carries the DAO type script.
6. The `OutputsSumOverflow` block is skipped entirely.
7. The DAO type script runs, verifies Output 0 is a valid withdrawal, and exits successfully.
8. No check compares the 50 CKB ordinary input against the 100 CKB ordinary output.
9. The transaction is committed; 50 CKB has been minted from nothing.

**Root cause lines:** [1](#0-0) [2](#0-1)

### Citations

**File:** verification/src/transaction_verifier.rs (L479-494)
```rust
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
