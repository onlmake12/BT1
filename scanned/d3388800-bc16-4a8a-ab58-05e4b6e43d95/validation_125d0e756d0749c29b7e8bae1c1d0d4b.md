### Title
`CapacityVerifier` Skips Entire Capacity Balance Check for Mixed DAO/Non-DAO Transactions, Enabling Capacity Inflation — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check (inputs ≥ outputs) for **any** transaction that contains **at least one** DAO input cell. The DAO type script only enforces DAO-specific capacity rules for its own cells; it does not enforce the overall transaction balance. A transaction sender can mix one DAO input with arbitrary non-DAO inputs and set outputs that exceed the total inputs, creating CKB capacity out of thin air.

---

### Finding Description

In `verification/src/transaction_verifier.rs`, `CapacityVerifier::verify()` contains the following guard:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(...OutputsSumOverflow...);
    }
}
``` [1](#0-0) 

`valid_dao_withdraw_transaction()` returns `true` if **any** resolved input carries the DAO type script:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

When this returns `true`, the **entire** `OutputsSumOverflow` check is bypassed for the whole transaction — including all non-DAO inputs. The developer comment says "DAO withdraw transaction is verified via the type script of DAO cells," but the DAO type script (`dao.c`) only validates the DAO-specific capacity calculation (maximum withdrawal amount for the DAO cell). It does not enforce the global invariant `sum(inputs) >= sum(outputs)` across all cells in the transaction.

The DAO type script's capacity enforcement is limited to its own cells, as confirmed by `DaoCalculator::calculate_maximum_withdraw`, which computes the withdrawal amount for a single DAO output: [3](#0-2) 

Non-DAO inputs in the same transaction contribute their capacity to `inputs_sum` but are never checked against `outputs_sum` once the bypass is triggered.

---

### Impact Explanation

An attacker who controls a DAO cell (any amount, even minimal) can construct a phase-2 withdrawal transaction that also consumes non-DAO inputs and sets outputs exceeding the total inputs capacity. The `CapacityVerifier` skips the balance check; the DAO type script approves the DAO portion; no verifier checks the non-DAO shortfall. The result is **consensus-level capacity inflation**: CKB shannons are created from nothing, violating the fixed-supply invariant. This is a direct analog to the ERC20 fee-on-transfer accounting mismatch — the system assumes the DAO type script accounts for the full transaction balance, but it only accounts for the DAO cell's portion.

---

### Likelihood Explanation

Any unprivileged transaction sender with a DAO cell (obtainable by depositing any amount into the DAO) can trigger this. The attacker needs only to:
1. Deposit CKB into the DAO (phase 1).
2. Prepare the withdrawal (phase 2 phase 1).
3. Submit a withdrawal transaction with additional non-DAO inputs and inflated outputs.

No special privileges, no majority hashpower, no social engineering required. The attack is fully on-chain and deterministic.

---

### Recommendation

Replace the broad bypass with a precise check: the `OutputsSumOverflow` check should still be applied to the non-DAO portion of the transaction. Specifically, compute `non_dao_inputs_sum` (sum of capacities of inputs that are **not** DAO cells) and verify that `outputs_sum <= dao_maximum_withdraw + non_dao_inputs_sum`. Alternatively, enforce that `valid_dao_withdraw_transaction()` only suppresses the check when **all** inputs are DAO cells, not just any one of them.

---

### Proof of Concept

Construct a transaction:
- **Input 0**: A DAO phase-2 cell with 100 CKB (maximum withdrawal = 110 CKB with accrued interest).
- **Input 1**: A normal (non-DAO) cell with 50 CKB.
- **Output 0**: A single cell with 200 CKB.

`valid_dao_withdraw_transaction()` returns `true` because Input 0 uses the DAO type script. [2](#0-1) 

`CapacityVerifier::verify()` skips the `OutputsSumOverflow` check entirely. [4](#0-3) 

The DAO type script validates that the DAO output equals 110 CKB — but the output is 200 CKB, and no verifier checks the 40 CKB shortfall against the non-DAO input. The transaction passes consensus validation, and 40 CKB is created from nothing.

The `DaoCalculator::transaction_maximum_withdraw` correctly accounts for non-DAO inputs by returning `output.capacity().into()` for them: [5](#0-4) 

But this function is only used for fee calculation and DAO field computation — it is **not** called by `CapacityVerifier`. The capacity balance check is simply absent for mixed transactions.

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

**File:** util/dao/src/lib.rs (L127-158)
```rust
    pub fn calculate_maximum_withdraw(
        &self,
        output: &CellOutput,
        output_data_capacity: Capacity,
        deposit_header_hash: &Byte32,
        withdrawing_header_hash: &Byte32,
    ) -> Result<Capacity, DaoError> {
        let deposit_header = self
            .data_loader
            .get_header(deposit_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        let withdrawing_header = self
            .data_loader
            .get_header(withdrawing_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        if deposit_header.number() >= withdrawing_header.number() {
            return Err(DaoError::InvalidOutPoint);
        }

        let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
        let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());

        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

        Ok(withdraw_capacity)
```
