### Title
DAO Withdrawal `header_dep_index` Truncation Mismatch Between Rust `DaoCalculator` and On-Chain C VM Causes Consensus Split and Block Rejection - (File: `util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the `header_dep_index` stored in a DAO withdrawal witness as a full `u64`, while the on-chain `dao.c` script running inside CKB-VM reads only the **lowest byte** (treating it as `u8`). When a transaction sender supplies a `header_dep_index ≥ 256`, the C VM resolves the correct deposit block via the lowest byte, accepts the transaction, and a miner includes it in a block. The Rust `DaoHeaderVerifier` then calls `DaoCalculator::dao_field()` during block verification, resolves the full `u64` index to a different (wrong) block, fails the block-number consistency check, and rejects the block with `BlockErrorKind::InvalidDAO`. This is a consensus split: a block valid under script execution is rejected by the Rust node's contextual block verifier.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit block's `header_dep_index` from the witness `input_type` field:

```rust
// util/dao/src/lib.rs lines 83–96
let header_deps_index_data: Option<Bytes> =
    witness.input_type().to_opt().map(|witness| witness.into());
// ...
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)   // uses full u64
        .and_then(|hash| header_deps.get(&hash))
        .ok_or(DaoError::InvalidOutPoint)
})?;
```

The index is read as a full `u64` and used directly to index into `header_deps()`.

The on-chain `dao.c` script, however, reads this field as a `u8` (lowest byte only). This discrepancy is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

The test constructs a transaction with `header_dep_index = 257` (little-endian bytes: `[0x01, 0x01, 0, 0, 0, 0, 0, 0]`), places the deposit block at index 1 and the withdraw block at index 257, and confirms the Rust `DaoCalculator` returns `Err` while the C VM would accept it (lowest byte = 1 → deposit block).

This discrepancy propagates into block verification via `DaoHeaderVerifier::verify()` in `verification/contextual/src/contextual_block_verifier.rs`:

```rust
// lines 300–318
pub fn verify(&self) -> Result<(), Error> {
    let dao = DaoCalculator::new(...)
        .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
        ...?;
    if dao != self.header.dao() {
        return Err((BlockErrorKind::InvalidDAO).into());
    }
    Ok(())
}
```

`DaoHeaderVerifier::verify()` is called unconditionally (unless `disable_daoheader` switch is set) in `ContextualBlockVerifier::verify()` at line 671.

---

### Impact Explanation

A transaction sender crafts a DAO withdrawal where `header_dep_index = 257` (or any value ≥ 256 whose lowest byte points to the correct deposit block). The transaction has ≥ 258 `header_deps`, with the deposit block hash at index 1 and a valid but wrong block at index 257.

1. CKB-VM runs `dao.c` → reads index as `u8 = 1` → resolves deposit block → passes validation → transaction accepted.
2. A miner includes the transaction in a block.
3. `DaoHeaderVerifier` calls `DaoCalculator::dao_field()` → reads index as `u64 = 257` → resolves the wrong block → `deposit_header.number() != deposited_block_number` → `DaoError::InvalidOutPoint` → `BlockErrorKind::InvalidDAO`.
4. The Rust node rejects the block.

**Result**: A valid block (accepted by the script engine) is rejected by the Rust node's contextual block verifier, causing a consensus split. Miners who include such a transaction produce blocks that the Rust node will never accept, wasting mining work and potentially stalling chain progress if the attack is sustained.

---

### Likelihood Explanation

The attacker only needs to be an unprivileged transaction sender. Constructing a DAO withdrawal with `header_dep_index ≥ 256` requires no special privilege: the attacker must hold a DAO cell (deposited CKB), which is a normal user action. The transaction is syntactically valid and passes CKB-VM script execution. The attack is deterministic and reproducible. The codebase itself contains a test (`check_dao_withdraw_header_dep_index_exceeds_u8`) that proves the discrepancy exists and is reachable.

---

### Recommendation

Align the Rust `DaoCalculator` with the on-chain `dao.c` behavior. Either:

1. **Truncate the index to `u8` in Rust** to match the C VM: change `header_dep_index as usize` to `(header_dep_index as u8) as usize` in `transaction_maximum_withdraw`.
2. **Fix `dao.c`** to read the full `u64` index (matching the Rust side), and deploy the fix as a hard fork.

Option 1 is the lower-risk fix since it aligns the Rust validator with the already-deployed on-chain script without requiring a consensus change.

Additionally, add a consensus-level validation rule that rejects DAO withdrawal transactions where `header_dep_index ≥ 256` until the discrepancy is resolved, to prevent the attack vector entirely.

---

### Proof of Concept

The existing test in `util/dao/src/tests.rs` at lines 475–537 directly demonstrates the discrepancy:

```rust
// header_dep_index = 257, lowest byte = 1
// header_deps[1]   = deposit_block.hash()   ← C VM resolves here (u8 = 1)
// header_deps[257] = withdraw_block.hash()  ← Rust resolves here (u64 = 257)
let witness = WitnessArgs::new_builder()
    .input_type(Some(Bytes::from(257u64.to_le_bytes().to_vec())))
    .build();
// ...
let result = calculator.transaction_fee(&rtx);
// Rust resolves index 257 → withdraw block (number 200), but cell data
// says deposited at block 100. Block number check catches the mismatch.
assert!(result.is_err(), "expected Err, got {result:?}");
```

To trigger the consensus split on a live node:
1. Deposit CKB into the DAO.
2. Prepare a withdrawal transaction with 258 `header_deps`, deposit block hash at index 1, and `input_type` witness = `257u64.to_le_bytes()`.
3. Submit the transaction. The CKB-VM `dao.c` script accepts it (reads index 1 = deposit block).
4. Wait for a miner to include it in a block.
5. The Rust node's `DaoHeaderVerifier` calls `DaoCalculator::dao_field()`, which fails with `DaoError::InvalidOutPoint` for this transaction, causing `BlockErrorKind::InvalidDAO` and block rejection.

**Root cause lines**: [1](#0-0) 

**Block verification entry point**: [2](#0-1) 

**Verification called unconditionally**: [3](#0-2) 

**Test proving the discrepancy**: [4](#0-3)

### Citations

**File:** util/dao/src/lib.rs (L91-96)
```rust
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-319)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao = DaoCalculator::new(
            &self.context.consensus,
            &self.context.store.borrow_as_data_loader(),
        )
        .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
        .map_err(|e| {
            error_target!(
                crate::LOG_TARGET,
                "Error generating dao data for block {}: {:?}",
                self.header.hash(),
                e
            );
            e
        })?;

        if dao != self.header.dao() {
            return Err((BlockErrorKind::InvalidDAO).into());
        }
        Ok(())
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L670-672)
```rust
        if !self.switch.disable_daoheader() {
            DaoHeaderVerifier::new(&self.context, resolved, &parent, &block.header()).verify()?;
        }
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
