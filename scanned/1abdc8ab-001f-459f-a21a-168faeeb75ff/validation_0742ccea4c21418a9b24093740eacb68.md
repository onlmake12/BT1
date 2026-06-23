### Title
Skipped Capacity Balance Check in Mixed DAO/Non-DAO Withdrawal Transactions Allows Capacity Inflation - (File: verification/src/transaction_verifier.rs)

### Summary
`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for any transaction that contains at least one DAO-type input, delegating all verification to the DAO type script. However, the DAO type script only verifies the capacity of the specific DAO output cell (at the same index as the DAO input), not the total transaction balance. A transaction mixing DAO inputs with regular non-DAO inputs can therefore include non-DAO outputs with more capacity than the corresponding non-DAO inputs, creating CKB capacity out of thin air.

### Finding Description
In `verification/src/transaction_verifier.rs`, `CapacityVerifier::verify()` contains the following logic:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(...)
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

The code comment states: *"DAO withdraw transaction is verified via the type script of DAO cells."* However, the DAO type script only verifies the output at the **same index** as the DAO input — it checks that `output[i].capacity >= max_withdraw(input[i])` for each DAO input `i`. It does not verify the total transaction balance across all inputs and outputs, including non-DAO cells. [3](#0-2) 

The `DaoCalculator::calculate_maximum_withdraw` function computes the per-cell maximum withdrawal amount, which is what the DAO type script enforces. There is no mechanism that enforces `sum(all_outputs.capacity) <= sum(all_inputs.capacity)` for a DAO withdrawal transaction containing non-DAO cells. [4](#0-3) 

This is directly analogous to the reported SwiftSource bug: in that case, native ETH was transferred via `payEth` (direct transfer) while `feeManager.depositFee` expected `msg.value`, so the ETH was not recognized. Here, non-DAO capacity is present in the transaction but neither the `CapacityVerifier` (which skips the check) nor the DAO type script (which only checks DAO cells) accounts for it — the non-DAO capacity is not recognized by any accounting mechanism.

### Impact Explanation
An unprivileged transaction sender can create CKB capacity out of thin air. By including a DAO prepare input alongside one or more regular inputs in a withdrawal transaction, and setting regular outputs to exceed the regular inputs' capacity, the attacker inflates the total CKB supply. The DAO type script verifies only the DAO output cell; the `CapacityVerifier` skips the total balance check entirely. This breaks CKB's core monetary invariant that `sum(outputs.capacity) <= sum(inputs.capacity)` for non-cellbase transactions.

### Likelihood Explanation
The attacker must first go through the DAO deposit and prepare phases (two on-chain transactions, with the prepare phase requiring waiting for an epoch boundary). This is a normal user action and requires only existing CKB. Once the prepare cell is available, the attack is straightforward: submit a crafted withdrawal transaction via the standard `send_transaction` RPC. No privileged access, no majority hashpower, and no social engineering is required.

### Recommendation
The `OutputsSumOverflow` check should not be skipped for the entire transaction when a DAO input is present. Instead, the check should be applied to the non-DAO portion of the transaction (i.e., verify that `sum(non_dao_outputs.capacity) <= sum(non_dao_inputs.capacity)`), while the DAO type script continues to handle the DAO-specific cells. Alternatively, the DAO type script should be updated to also verify the total transaction balance including non-DAO cells.

### Proof of Concept

1. Deposit 100 CKB into the DAO (standard deposit transaction).
2. Submit a prepare transaction (standard DAO phase 1 withdrawal).
3. Wait for the required epoch boundary.
4. Craft a withdrawal transaction:
   - **Input 0**: DAO prepare cell (100 CKB, `max_withdraw` = 110 CKB)
   - **Input 1**: Regular cell (50 CKB, no type script)
   - **Output 0**: Regular cell, capacity = 110 CKB (DAO withdrawal — verified by DAO type script at index 0)
   - **Output 1**: Regular cell, capacity = 55 CKB (5 CKB extra — verified by **nobody**)
5. Submit via `send_transaction` RPC.
6. `valid_dao_withdraw_transaction()` returns `true` (Input 0 has DAO type script) → `CapacityVerifier` skips the total balance check.
7. The DAO type script verifies Output 0 = 110 CKB ✓. Output 1 = 55 CKB is unchecked.
8. Total inputs = 150 CKB; total outputs = 165 CKB → **15 CKB created out of thin air**. [5](#0-4) [2](#0-1)

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

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
    }
```

**File:** util/dao/src/lib.rs (L126-159)
```rust
    /// Calculate maximum withdraw capacity of a deposited dao output
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
    }
```
