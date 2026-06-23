### Title
`CapacityVerifier` Skips Whole-Transaction Capacity Balance for Mixed DAO/Non-DAO Withdrawal Transactions, Enabling Non-DAO Capacity Inflation — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for **any** transaction that contains at least one DAO-type-script input. The code comment asserts the NervosDAO type script handles this verification, but the DAO type script only enforces the withdrawal amount for the DAO cell itself. Non-DAO inputs co-present in the same withdrawal transaction receive no capacity balance enforcement, allowing an attacker to inflate non-DAO output capacity beyond non-DAO input capacity — creating CKB from nothing.

---

### Finding Description

In `verification/src/transaction_verifier.rs`, `CapacityVerifier::verify()` contains:

```rust
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

When this returns `true`, the **entire** `OutputsSumOverflow` check is skipped for the whole transaction — including any non-DAO inputs. The justifying comment states "DAO withdraw transaction is verified via the type script of DAO cells." However, the NervosDAO type script (a system script) only verifies the withdrawal amount for the DAO cell itself; it does not enforce that the total output capacity of the transaction is bounded by the total input capacity.

This is structurally identical to the tBTC analog: code assumes a balance/enforcement exists in a downstream component (the DAO type script), but that component only covers a subset of the accounting (the DAO cell), leaving the remainder (non-DAO inputs) unguarded.

The `transaction_maximum_withdraw` function in `util/dao/src/lib.rs` confirms that non-DAO inputs are treated as raw capacity pass-throughs:

```rust
} else {
    Ok(output.capacity().into())  // non-DAO input: raw capacity, no interest
}
``` [3](#0-2) 

This value is used only for fee calculation (`transaction_fee`), not for enforcing the capacity balance during verification. The `CapacityVerifier` is the sole enforcement point for `OutputsSumOverflow`, and it is bypassed.

---

### Impact Explanation

An attacker who holds a DAO cell (phase-2 withdrawal ready) and any non-DAO cell can construct a transaction:

| Side | Cell | Capacity |
|------|------|----------|
| Input | DAO cell (phase-2) | 100 CKB + interest |
| Input | Non-DAO cell | 50 CK

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

**File:** util/dao/src/lib.rs (L117-119)
```rust
                    } else {
                        Ok(output.capacity().into())
                    }
```
