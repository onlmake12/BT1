### Title
`CapacityVerifier::valid_dao_withdraw_transaction` Skips Entire Capacity Balance Check for Mixed DAO/Non-DAO Transactions, Enabling Capacity Inflation - (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for any transaction that contains **at least one** DAO-type input. The DAO type script (C VM) only enforces the capacity constraint for the DAO cell itself, not for the whole transaction. A transaction sender who owns any DAO cell can therefore combine it with non-DAO inputs and set outputs that exceed total inputs capacity, minting CKB capacity from thin air.

---

### Finding Description

In `verification/src/transaction_verifier.rs`, `CapacityVerifier::verify()` guards the `OutputsSumOverflow` check with:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(TransactionError::OutputsSumOverflow { ... }.into());
    }
}
```

`valid_dao_withdraw_transaction()` returns `true` if **any** input carries the DAO type script:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
```

The comment at line 482 states: *"DAO withdraw transaction is verified via the type script of DAO cells."* This assumption is incorrect for mixed transactions. The DAO type script (the on-chain C script) only verifies that the **DAO cell's own output capacity** equals the calculated maximum withdraw. It does not verify `sum(all outputs) ≤ sum(all inputs)`. No other verifier in the pipeline enforces this invariant for mixed transactions.

Consequently, a transaction with:
- **Input A**: a DAO cell (any phase) with capacity `D`
- **Input B**: a regular non-DAO cell with capacity `R`
- **Output 1**: DAO withdraw output with capacity `D + interest` (passes DAO type script)
- **Output 2**: regular output with capacity `R + X` (no script checks this)

…passes all verifiers. The attacker gains `X` shannons of capacity that did not exist in the inputs.

For a deposit-phase DAO cell (`deposited_block_number == 0`), `transaction_maximum_withdraw` returns `output.capacity()` (line 115 of `util/dao/src/lib.rs`), so even a freshly deposited DAO cell suffices as the trigger input.

---

### Impact Explanation

An unprivileged transaction sender who owns any DAO cell (a normal user action) can submit a crafted transaction that inflates total output capacity beyond total input capacity. This violates the fundamental CKB conservation law (`sum(outputs) ≤ sum(inputs)`) and allows minting of CKB capacity from thin air. At scale this is a consensus-breaking inflation attack.

---

### Likelihood Explanation

Any CKB user who has ever deposited into NervosDAO owns a DAO cell and can trigger this path. The attack requires only constructing a valid transaction with mixed inputs and submitting it via `send_transaction` RPC or P2P relay. No privileged access, no majority hashpower, and no social engineering is required.

---

### Recommendation

`valid_dao_withdraw_transaction()` should not be used as a blanket bypass for the entire `OutputsSumOverflow` check. Instead, the verifier should enforce:

```
sum(non-DAO outputs) ≤ sum(non-DAO inputs)
```

separately from the DAO-specific capacity check, or alternatively compute the expected maximum total output capacity as `sum(non-DAO inputs) + sum(DAO maximum withdraws)` and compare against `sum(all outputs)`. The DAO type script's enforcement scope is limited to the DAO cell itself and cannot substitute for a whole-transaction balance check.

---

### Proof of Concept

1. User deposits 1000 CKB into NervosDAO → owns a DAO cell with capacity 1000 CKB.
2. User also owns a regular cell with capacity 500 CKB.
3. User constructs a phase-2 DAO withdraw transaction:
   - Input 0: DAO cell (1000 CKB, phase 2 prepare cell)
   - Input 1: regular cell (500 CKB)
   - Output 0: DAO withdraw output (1000 + interest CKB) — passes DAO type script
   - Output 1: regular output (600 CKB) — 100 CKB more than the 500 CKB input
4. `CapacityVerifier::valid_dao_withdraw_transaction()` returns `true` (Input 0 is a DAO cell).
5. The `OutputsSumOverflow` check is skipped entirely.
6. The DAO type script verifies only Output 0's capacity.
7. Transaction is accepted; user has created 100 CKB from nothing. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** util/dao/src/lib.rs (L108-113)
```rust
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
```

**File:** util/dao/src/lib.rs (L114-119)
```rust
                        } else {
                            Ok(output.capacity().into())
                        }
                    } else {
                        Ok(output.capacity().into())
                    }
```
