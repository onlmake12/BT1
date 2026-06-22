### Title
`CapacityVerifier` Skips Total Capacity Overflow Check for DAO Transactions While DAO Type Script Only Enforces DAO-Cell Capacity — (`verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for any transaction that contains at least one DAO-type input, delegating enforcement to the DAO type script. However, the DAO type script only verifies the capacity of the DAO cells themselves, not the total capacity balance of the transaction. Non-DAO outputs in the same transaction are verified by neither the `CapacityVerifier` nor the DAO script, creating a gap through which a block producer can inflate non-DAO outputs beyond non-DAO inputs — creating CKB capacity out of thin air.

---

### Finding Description

`CapacityVerifier::verify()` contains the following guard:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(...)
    }
}
``` [1](#0-0) 

`valid_dao_withdraw_transaction()` returns `true` if **any** input cell uses the DAO type script:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

The code comment states: *"DAO withdraw transaction is verified via the type script of DAO cells."* [3](#0-2) 

The DAO type script (`DaoCalculator::calculate_maximum_withdraw`) only computes and enforces the withdrawal amount for the DAO cell itself — it does not inspect or constrain non-DAO outputs in the same transaction:

```rust
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
Ok(withdraw_capacity)
``` [4](#0-3) 

**Attack scenario:**

| Item | Capacity |
|---|---|
| DAO input (prepare phase) | 100 CKB |
| Non-DAO input | 50 CKB |
| DAO output (with interest, verified by DAO script) | 110 CKB |
| Non-DAO output (unverified) | 60 CKB |
| **Net inflation** | **+20 CKB** |

- `CapacityVerifier` skips the total overflow check because a DAO input is present.
- The DAO type script verifies only the DAO cell (100 → 110 CKB with interest). It does not inspect the non-DAO output.
- No verifier checks that the non-DAO output (60 CKB) is bounded by the non-DAO input (50 CKB).
- All full nodes accept the block, permanently inflating the CKB supply.

This is structurally identical to M-01: the validation step (`CapacityVerifier`) assumes a second mechanism (the DAO script) will enforce the full constraint, but that second mechanism only enforces a subset of it (DAO cells only), leaving the remainder (non-DAO capacity balance) unchecked.

---

### Impact Explanation

A block producer who includes such a crafted transaction causes all honest nodes to accept a block that violates the fundamental CKB invariant that `sum(inputs_capacity) ≥ sum(outputs_capacity)`. The result is permanent, consensus-accepted inflation of the CKB token supply. Because the `CapacityVerifier` is part of the shared block-verification pipeline, every node on the network accepts the inflated block without error. The stolen capacity can be directed to any address the attacker controls.

---

### Likelihood Explanation

The tx-pool's `check_tx_fee` (via `DaoCalculator::transaction_fee`) does catch this for transactions submitted through the normal RPC path, because `safe_sub` fails when outputs exceed the maximum withdraw:

```rust
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))
        .map_err(Into::into)
}
``` [5](#0-4) 

However, a block producer can bypass the tx-pool entirely and inject the transaction directly into a block they assemble. CKB mining is permissionless — any node can be a miner — and a single block is sufficient to exploit this. No majority hashpower is required.

---

### Recommendation

The `OutputsSumOverflow` check should not be skipped wholesale for DAO transactions. Instead, the verifier should:

1. Apply the standard `inputs_sum ≥ outputs_sum` check to the **non-DAO portion** of the transaction (i.e., subtract the DAO cells' contribution from both sides before comparing), or
2. Verify that `total_outputs_capacity ≤ total_non_dao_inputs_capacity + dao_maximum_withdraw` within `CapacityVerifier` itself, rather than delegating the entire check to the DAO type script.

---

### Proof of Concept

Craft a DAO phase-2 withdrawal transaction with:
- One DAO input cell (e.g., 100 CKB, with cell data encoding the deposit block number)
- One non-DAO input cell (e.g., 50 CKB)
- One DAO output cell (e.g., 110 CKB — within the DAO script's allowed maximum)
- One non-DAO output cell (e.g., 60 CKB — 10 CKB more than the non-DAO input)

Submit this transaction directly to a block assembler (bypassing the tx-pool). The resulting block passes `CapacityVerifier` (overflow check skipped due to DAO input) and passes DAO script execution (DAO cell correctly bounded). All nodes accept the block. The attacker has created 10 CKB from nothing.

The existing test `test_skip_dao_capacity_check` in `verification/src/tests/transaction_verifier.rs` already demonstrates that `CapacityVerifier` passes for a DAO transaction with zero inputs and a 500 CKB output — confirming the skip is unconditional: [6](#0-5)

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

**File:** util/dao/src/lib.rs (L149-158)
```rust
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

**File:** verification/src/tests/transaction_verifier.rs (L164-186)
```rust
#[test]
pub fn test_skip_dao_capacity_check() {
    let dao_type_script = build_genesis_type_id_script(OUTPUT_INDEX_DAO);
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .capacity(capacity_bytes!(500))
                .type_(Some(dao_type_script.clone()))
                .build(),
        )
        .output_data(Bytes::new())
        .build();

    let rtx = Arc::new(ResolvedTransaction {
        transaction,
        resolved_cell_deps: Vec::new(),
        resolved_inputs: vec![],
        resolved_dep_groups: vec![],
    });
    let verifier = CapacityVerifier::new(rtx, dao_type_script.calc_script_hash());

    assert!(verifier.verify().is_ok());
}
```
