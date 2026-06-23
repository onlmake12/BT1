### Title
DAO Lock Script Size Restriction Bypass via Index Misalignment in `DaoScriptSizeVerifier` - (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier` enforces that a NervosDAO deposit cell and its corresponding prepare cell use lock scripts of the same size. However, the verifier pairs inputs and outputs strictly by position using `.zip()`. A transaction sender can bypass this check entirely by placing the DAO deposit cell and the DAO prepare cell at non-matching indices within the same transaction, allowing the prepare cell to carry a smaller lock script than the deposit cell and thereby inflating the maximum withdrawal amount.

---

### Finding Description

`DaoScriptSizeVerifier::verify()` iterates over `resolved_inputs.iter().zip(transaction.outputs())`:

```rust
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())
    .enumerate()
{
    if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
        && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
    {
        continue;
    }
    ...
    if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
        return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
    }
}
```

The check fires only when **both** the input at index `i` **and** the output at index `i` use the DAO type script. A transaction sender can trivially defeat this by placing the DAO deposit cell and the DAO prepare cell at different indices:

```
inputs[0]  = DAO deposit cell  (large lock script, e.g. 100 bytes)
inputs[1]  = non-DAO fee cell

outputs[0] = non-DAO change cell
outputs[1] = DAO prepare cell  (small lock script, e.g. 20 bytes)
```

The zip produces pairs `(inputs[0], outputs[0])` and `(inputs[1], outputs[1])`. Neither pair has both cells using the DAO type script, so the check is skipped for both. The DAO deposit cell and the DAO prepare cell are never compared.

The on-chain `dao.c` script does not check lock script sizes — the comment in the source explicitly acknowledges this: *"It provides a temporary solution till Nervos DAO script can be properly upgraded."* The Rust verifier is the **only** enforcement layer, and it is bypassable.

---

### Impact Explanation

The withdrawal amount is calculated in `DaoCalculator::calculate_maximum_withdraw()`:

```rust
let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
let output_capacity: Capacity = output.capacity().into();
let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

`occupied_capacity` is derived from the prepare cell's lock script size. A smaller lock script → lower `occupied_capacity` → higher `counted_capacity` → higher `withdraw_counted_capacity` → higher total withdrawal. By bypassing the size check, an attacker can deposit with a large lock script and prepare with a small one, claiming more CKB than they are entitled to. This is a direct theft of secondary issuance from the NervosDAO pool, affecting all depositors.

Additionally, `CapacityVerifier` skips the `inputs_sum >= outputs_sum` check for any transaction that has a DAO input (`valid_dao_withdraw_transaction()` returns true if any input uses the DAO type script), so there is no secondary capacity guard to catch the mismatch.

---

### Likelihood Explanation

Any unprivileged transaction sender who has deposited CKB into NervosDAO can exploit this. The attack requires only crafting a standard phase-1 (deposit→prepare) transaction with the DAO cells at non-matching indices. No special access, no key compromise, no majority hashpower. The transaction is valid from the perspective of `dao.c` (capacity is preserved) and passes all other verifiers. The only guard is `DaoScriptSizeVerifier`, which is bypassed by index misalignment.

---

### Recommendation

Replace the index-coupled `.zip()` iteration with a check that matches DAO deposit inputs to DAO prepare outputs by type script membership, independent of position. Specifically, collect all DAO deposit inputs (data = all zeros, committed after `starting_block_limiting_dao_withdrawing_lock`) and all DAO prepare outputs, then verify that the multiset of lock script sizes is identical between the two groups. Alternatively, enforce a strict structural rule: DAO deposit inputs and their corresponding prepare outputs must appear at the same index, and reject any transaction that violates this layout.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` already demonstrates the general pattern of index-based discrepancies in DAO transactions. The bypass here follows the same structural principle applied to `DaoScriptSizeVerifier`.

Craft a phase-1 transaction:

```
inputs:
  [0] DAO deposit cell, lock = Script { args: [0u8; 100] }  // large lock
  [1] non-DAO cell (fee source)

outputs:
  [0] non-DAO change cell
  [1] DAO prepare cell, lock = Script { args: [0u8; 20] }   // small lock
       data = deposit_block_number (8 bytes LE)
       capacity = same as deposit cell (enforced by dao.c)
```

`DaoScriptSizeVerifier` iterates:
- pair `(inputs[0]=DAO deposit, outputs[0]=non-DAO)` → output not DAO → `continue`
- pair `(inputs[1]=non-DAO, outputs[1]=DAO prepare)` → input not DAO → `continue`

No `DaoLockSizeMismatch` error is raised. The `dao.c` script passes because capacity is preserved. The prepare cell is committed with a 20-byte lock script instead of the 100-byte deposit lock script. In phase 2, `calculate_maximum_withdraw` uses the smaller `occupied_capacity`, yielding a withdrawal amount larger than the depositor is entitled to. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** verification/src/transaction_verifier.rs (L845-890)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao_type_hash = self.dao_type_hash();
        for (i, (input_meta, cell_output)) in self
            .resolved_transaction
            .resolved_inputs
            .iter()
            .zip(self.resolved_transaction.transaction.outputs())
            .enumerate()
        {
            // Both the input and output cell must use Nervos DAO as type script
            if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
                && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
            {
                continue;
            }

            // A Nervos DAO deposit cell must have input data
            let input_data = match self.data_loader.load_cell_data(input_meta) {
                Some(data) => data,
                None => continue,
            };

            // Only input data with full zeros are counted as deposit cell
            if input_data.into_iter().any(|b| b != 0) {
                continue;
            }

            // Only cells committed after the pre-defined block number in consensus is
            // applied to this rule
            if let Some(info) = &input_meta.transaction_info
                && info.block_number
                    < self
                        .consensus
                        .starting_block_limiting_dao_withdrawing_lock()
            {
                continue;
            }

            // Now we have a pair of DAO deposit and withdrawing cells, it is expected
            // they have the lock scripts of the same size.
            if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
                return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
            }
        }
        Ok(())
    }
```

**File:** util/dao/src/lib.rs (L149-156)
```rust
        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L28-31)
```rust
use ckb_verification::{
    BlockErrorKind, CellbaseError, CommitError, ContextualTransactionVerifier,
    DaoScriptSizeVerifier, TimeRelativeTransactionVerifier, UnknownParentError,
};
```

**File:** util/dao/src/tests.rs (L475-537)
```rust
#[test]
fn check_dao_withdraw_header_dep_index_exceeds_u8() {
    let deposit_number = 100u64;
    let withdraw_number = 200u64;

    let (_tmp_dir, store, deposit_block, withdraw_block) =
        setup_store_with_headers(deposit_number, withdraw_number);

    let consensus = Consensus::default();
    let dao_type_script = Script::new_builder()
        .code_hash(consensus.dao_type_hash())
        .hash_type(ScriptHashType::Type)
        .build();

    // Pad header_deps to 258 entries so index 257 is valid.
    // Position 1: correct deposit block (what C VM resolves via lowest byte).
    // Position 257: withdraw block (wrong — Rust resolves this with full u64).
    let dummy = h256!("0x1").into();
    let mut header_deps = vec![dummy; 258];
    header_deps[1] = deposit_block.hash();
    header_deps[257] = withdraw_block.hash();

    let cell_data = Bytes::from(deposit_number.to_le_bytes().to_vec());
    let input_cell = CellOutput::new_builder()
        .capacity(capacity_bytes!(1000000))
        .type_(Some(dao_type_script).pack())
        .build();
    let tx_info = TransactionInfo::new(
        withdraw_block.number(),
        withdraw_block.epoch(),
        withdraw_block.hash(),
        0,
    );
    let cell_meta = CellMetaBuilder::from_cell_output(input_cell, cell_data)
        .transaction_info(tx_info)
        .build();

    // input_type = 257, lowest byte = 1
    let witness = WitnessArgs::new_builder()
        .input_type(Some(Bytes::from(257u64.to_le_bytes().to_vec())))
        .build();
    let witness_bytes: Bytes = witness.as_bytes();

    let tx = TransactionBuilder::default()
        .set_header_deps(header_deps)
        .witness(witness_bytes)
        .build();

    let rtx = ResolvedTransaction {
        transaction: tx,
        resolved_cell_deps: vec![],
        resolved_inputs: vec![cell_meta],
        resolved_dep_groups: vec![],
    };

    let data_loader = store.borrow_as_data_loader();
    let calculator = DaoCalculator::new(&consensus, &data_loader);
    let result = calculator.transaction_fee(&rtx);

    // Rust resolves index 257 → withdraw block (number 200), but cell data
    // says deposited at block 100. Block number check catches the mismatch.
    assert!(result.is_err(), "expected Err, got {result:?}");
}
```
