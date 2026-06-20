### Title
`CapacityVerifier` Skips Entire Capacity Overflow Check for Any Transaction Containing a DAO Input, Enabling CKB Inflation via Mixed DAO/Regular Cells — (`verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for the **entire transaction** whenever `valid_dao_withdraw_transaction()` returns `true`. That helper returns `true` if **any single input cell** carries the DAO type script. Because the DAO type script itself only audits DAO-specific cells, the capacity of all non-DAO inputs and outputs in the same transaction goes completely unchecked, allowing an attacker to manufacture CKB capacity from nothing.

---

### Finding Description

`CapacityVerifier::verify()` contains the following guard:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum  = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(TransactionError::OutputsSumOverflow { … }.into());
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

The `.any()` predicate means **one DAO input is sufficient** to suppress the capacity check for the whole transaction. The inline comment reads:

> *"DAO withdraw transaction is verified via the type script of DAO cells"* [3](#0-2) 

The DAO type script (RFC-0023) only audits the DAO-specific cells — it verifies that the withdrawal output carries at least `principal + accrued_interest`. It has no visibility into, and makes no assertions about, the capacity of co-located regular (non-DAO) cells. Consequently, for a transaction that mixes DAO inputs with ordinary inputs, **no code path enforces that regular outputs ≤ regular inputs**.

The per-output minimum-occupied-capacity check (lines 496–512) is still executed, so outputs cannot be under-sized, but they can be arbitrarily over-sized relative to the inputs. [4](#0-3) 

---

### Impact Explanation

An attacker who controls a DAO cell can craft a transaction that inflates the total CKB supply:

- **Inputs**: 1 DAO cell (e.g. 100 CKB) + 1 regular cell (e.g. 50 CKB) = 150 CKB total input
- **Outputs**: 1 DAO withdrawal cell (e.g. 110 CKB, passing DAO script) + 1 regular cell (e.g. 100 CKB) = 210 CKB total output

The DAO type script accepts the 110 CKB withdrawal output. `CapacityVerifier` skips the global sum check. The 50 CKB surplus on the regular output is never caught. The attacker has minted 60 CKB from nothing. Repeated across many transactions this constitutes an unbounded supply inflation attack — a direct consensus violation.

---

### Likelihood Explanation

Any CKB holder who has ever deposited into Nervos DAO (a common, documented operation) possesses the prerequisite DAO cell. No privileged role, leaked key, or majority hashpower is required. The attacker submits a single crafted transaction through the normal RPC or P2P relay path. The attack is fully permissionless and repeatable.

---

### Recommendation

Replace the coarse `.any()` guard with a capacity check that accounts for the DAO interest delta explicitly:

1. Compute `dao_interest` = sum of `(withdrawal_output_capacity − deposit_input_capacity)` for all DAO cell pairs in the transaction.
2. Enforce `inputs_sum + dao_interest >= outputs_sum` unconditionally, rather than skipping the check entirely.

Alternatively, keep the skip only when **all** inputs are DAO cells, so that mixed transactions always go through the standard overflow check.

---

### Proof of Concept

1. Deposit 100 CKB into Nervos DAO; wait for maturity.
2. Construct a transaction:
   - **Input 0**: DAO cell (100 CKB, DAO type script) — triggers `valid_dao_withdraw_transaction() == true`
   - **Input 1**: Regular cell owned by attacker (50 CKB)
   - **Output 0**: DAO withdrawal cell (110 CKB = principal + interest) — passes DAO type script
   - **Output 1**: Regular cell owned by attacker (100 CKB)
3. Submit via `send_transaction` RPC.
4. `CapacityVerifier::verify()` enters the `valid_dao_withdraw_transaction()` branch and skips the sum check entirely.
5. The DAO type script runs only against Output 0 and passes.
6. The block is accepted; the attacker has created 60 CKB (210 − 150) from nothing.

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

**File:** verification/src/transaction_verifier.rs (L496-513)
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
