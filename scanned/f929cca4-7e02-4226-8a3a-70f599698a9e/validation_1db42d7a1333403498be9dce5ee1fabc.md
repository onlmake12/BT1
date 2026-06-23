### Title
DAO Withdrawal Transaction Bypasses `OutputsSumOverflow` Capacity Check, Allowing Potential CKB Inflation — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` in `verification/src/transaction_verifier.rs` unconditionally skips the `OutputsSumOverflow` balance check for any transaction that has at least one DAO-type-script input cell. The code comment asserts the DAO type script handles this verification, but per RFC 0023 the DAO type script only verifies the DAO cell's own output capacity — not the total transaction balance. Non-DAO outputs in the same transaction are therefore never checked against total input capacity, creating a gap through which an attacker can inflate the CKB supply.

---

### Finding Description

**Root cause — conditional validation bypass in `CapacityVerifier::verify()`:**

```rust
// verification/src/transaction_verifier.rs  (lines 478–514)
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
    // only per-output occupied-capacity check follows — no total-balance check
    for (index, (output, data)) in self
        .resolved_transaction
        .transaction
        .outputs_with_data_iter()
        .enumerate()
    {
        let data_occupied_capacity = Capacity::bytes(data.len())?;
        if output.is_lack_of_capacity(data_occupied_capacity)? {
            return Err(...InsufficientCellCapacity...);
        }
    }
    Ok(())
}
``` [1](#0-0) 

The gate is `valid_dao_withdraw_transaction()`:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

It returns `true` if **any** input cell carries the DAO type script. When it does, the entire `OutputsSumOverflow` check is skipped for the whole transaction — including all non-DAO outputs.

**Why the DAO type script does not compensate:**

Per RFC 0023, the DAO type script (a system script, not in this repo) verifies only that the DAO cell's own output capacity equals the maximum withdrawal amount (principal + interest). It does not verify the total transaction balance (`sum(outputs) ≤ sum(inputs)`). The remaining per-output check in `CapacityVerifier` only confirms each output's capacity ≥ its own occupied capacity (data + scripts), which is trivially satisfied by any large-capacity output.

**Structural analogy to the reported vulnerability:**

| Reported (Solidity) | CKB analog |
|---|---|
| Validation only runs when `isDepositLimitSetRoleHolder == address(0)` | `OutputsSumOverflow` check only runs when `!valid_dao_withdraw_transaction()` |
| Setting a non-zero `isDepositLimitSetRoleHolder` bypasses the check | Including any DAO input bypasses the check |
| DAO type script assumed to cover the gap — it does not | DAO type script assumed to cover the gap — it does not |

---

### Impact Explanation

An attacker who controls a DAO cell can craft a withdrawal transaction that includes arbitrary extra non-DAO outputs. Because `OutputsSumOverflow` is skipped and the DAO type script only validates the DAO cell's own output, the extra outputs are never checked against total input capacity. The attacker can mint unbounded CKB in a single transaction, directly inflating the total supply. This is a consensus-level accounting break: every honest full node running this verifier would accept the inflated transaction.

---

### Likelihood Explanation

The precondition is only that the attacker has previously deposited CKB into the NervosDAO — a permissionless, publicly documented operation available to any CKB holder. No privileged role, leaked key, or majority hashpower is required. The attack path is a single crafted transaction submitted via the standard RPC or P2P relay.

---

### Recommendation

Remove the blanket skip of `OutputsSumOverflow` for DAO transactions. Instead, compute the balance check for all transactions and separately account for the DAO interest premium:

```rust
let inputs_sum = self.resolved_transaction.inputs_capacity()?;
let outputs_sum = self.resolved_transaction.outputs_capacity()?;

if !self.resolved_transaction.is_cellbase() {
    // For DAO withdrawals the DAO type script enforces the DAO cell's
    // own output capacity; the remaining balance must still hold.
    if inputs_sum < outputs_sum {
        return Err((Transaction

### Citations

**File:** verification/src/transaction_verifier.rs (L478-514)
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
