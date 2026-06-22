### Title
Missing `OutputsSumOverflow` Validation for Mixed DAO/Non-DAO Withdrawal Transactions - (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for **any** transaction that contains at least one DAO-type-script input. The comment delegates enforcement to the DAO type script, but the DAO type script only validates the DAO-cell-specific withdrawal amount — it does not enforce the global invariant that total outputs capacity ≤ total inputs capacity. Non-DAO inputs co-present in the same withdrawal transaction are therefore subject to no capacity-overflow guard, allowing an attacker to inflate the non-DAO output capacity beyond what the non-DAO inputs supply.

---

### Finding Description

In `CapacityVerifier::verify()`:

```rust
// verification/src/transaction_verifier.rs  (lines ~478-494)
pub fn verify(&self) -> Result<(), Error> {
    // skip OutputsSumOverflow verification for resolved cellbase and DAO
    // withdraw transactions.
    // cellbase's outputs are verified by RewardVerifier
    // DAO withdraw transaction is verified via the type script of DAO cells
    if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
        let inputs_sum = self.resolved_transaction.inputs_capacity()?;
        let outputs_sum = self.resolved_transaction.outputs_capacity()?;
        if inputs_sum < outputs_sum {
            return Err((TransactionError::OutputsSumOverflow { inputs_sum, outputs_sum }).into());
        }
    }
    // InsufficientCellCapacity check still runs for all outputs …
}
``` [1](#0-0) 

The gate condition is:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

`any(…)` — not `all(…)` — means a single DAO input in a transaction that also carries arbitrary non-DAO inputs is sufficient to suppress the entire `OutputsSumOverflow` check for the whole transaction.

The DAO type script (RFC 0023) verifies only the per-DAO-cell invariant: `output_capacity = deposited_capacity × (AR_m / AR_n)`. It does **not** verify the global balance `Σ outputs ≤ Σ inputs`. The non-DAO inputs and outputs in the same transaction are therefore unchecked by any capacity-conservation rule.

This is structurally identical to the reported Masset issue: one redemption path (`_redeemMasset`) enforces the collateralisation ratio; the sibling path (`_redeemTo`) does not, relying on an external actor to uphold the invariant. Here, the non-DAO path of a mixed withdrawal transaction has no enforcer.

---

### Impact Explanation

An attacker who holds any DAO deposit can construct a withdrawal transaction that also spends non-DAO inputs and produces non-DAO outputs whose total capacity exceeds the non-DAO inputs' capacity. The difference is capacity created from nothing. Because this bypasses the consensus-level capacity-conservation invariant, it constitutes **inflation of the native CKB token** — a critical consensus violation.

---

### Likelihood Explanation

The attack requires only:
1. A prior DAO deposit (any amount, any duration).
2. Any non-DAO live cell to co-spend.
3. Submission of a crafted transaction via the standard `send_transaction` RPC.

No privileged access, no majority hash power, no social engineering. The entry path is fully reachable by an unprivileged transaction sender.

---

### Recommendation

Replace the blanket skip with a split check:

- Continue to skip the DAO-cell portion of the capacity balance (since DAO interest legitimately increases output capacity).
- Apply `OutputsSumOverflow` to the **non-DAO** portion: `Σ non-DAO outputs ≤ Σ non-DAO inputs`.

Alternatively, require that `valid_dao_withdraw_transaction` uses `all(…)` (every input is a DAO cell) before suppressing the check, and reject mixed transactions at the pool-admission layer.

---

### Proof of Concept

1. Deposit 100 CKB into the DAO → DAO cell A (input for withdrawal).
2. Hold a separate live cell B with 50 CKB (non-DAO).
3. Construct a withdrawal transaction:
   - **Inputs**: DAO cell A (100 CKB) + cell B (50 CKB) → total inputs = 150 CKB.
   - **Outputs**: withdrawal cell (105 CKB, DAO interest) + attacker cell (200 CKB).
   - Total outputs = 305 CKB >> 150 CKB inputs.
4. `valid_dao_withdraw_transaction()` returns `true` (cell A has DAO type script) → `OutputsSumOverflow` check is skipped entirely.
5. DAO type script runs on cell A's output and confirms 105 CKB is correct.
6. `InsufficientCellCapacity` passes (each output individually has enough capacity for its data).
7. Transaction is accepted; 155 CKB is created from nothing. [3](#0-2) [4](#0-3)

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

**File:** verification/src/transaction_verifier.rs (L517-533)
```rust
    fn valid_dao_withdraw_transaction(&self) -> bool {
        self.resolved_transaction
            .resolved_inputs
            .iter()
            .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
    }
}

fn cell_uses_dao_type_script(cell_output: &CellOutput, dao_type_hash: &Byte32) -> bool {
    cell_output
        .type_()
        .to_opt()
        .map(|t| {
            Into::<u8>::into(t.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
                && &t.code_hash() == dao_type_hash
        })
        .unwrap_or(false)
```
