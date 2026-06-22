### Title
DAO Withdrawal `header_dep_index` Truncation Mismatch Between C Script and Rust Verifier Causes Wrong Deposit-Header Lookup — (`File: util/dao/src/lib.rs`)

### Summary

The Rust `DaoCalculator` reads the `header_dep_index` from the witness as a full `u64`, while the on-chain C DAO script reads only the lowest byte (`u8`). When a transaction encodes an index value greater than 255, the two implementations resolve to different entries in `header_deps`, causing the Rust verifier to look up the wrong deposit block header. This is the direct CKB analog of the reported Astaria bug: using the wrong index/identifier when performing a lookup from an indirection array, leading to incorrect accounting.

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` iterates over resolved inputs and, for each DAO-type cell, reads the `header_dep_index` from the witness `input_type` field as a little-endian `u64`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then uses this full `u64` value to index into `header_deps()`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
```

The on-chain C DAO script (`ckb-system-scripts/c/dao.c`) reads the same 8-byte field but interprets only the **lowest byte** as the index (effectively treating it as `u8`). This is explicitly documented in the test file:

> "Position 1: correct deposit block (what C VM resolves via lowest byte)."
> "input_type = 257, lowest byte = 1"

When `header_dep_index = 257` (0x0000000000000101):
- C VM resolves to `header_deps[1]` (the deposit block)
- Rust resolves to `header_deps[257]` (a different block)

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` constructs exactly this scenario with 258 `header_deps` entries and asserts that Rust rejects the transaction because the block at index 257 has a different block number than what is stored in cell data.

### Impact Explanation

Two concrete impact paths exist:

**Path 1 — Consensus split / transaction rejection DoS:** A transaction that the C DAO script (executed by CKB-VM) accepts — because it correctly resolves `index & 0xFF` to the deposit block — is rejected by the Rust `DaoCalculator` because it resolves the full `u64` index to a different block whose number does not match the cell data. If `DaoCalculator::transaction_fee` is called in the block-verification path (as it is in the contextual verifier), the Rust node rejects a block that CKB-VM's script execution approved, causing an intra-node inconsistency or cross-node consensus split.

**Path 2 — Wrong maximum-withdraw accounting:** If an attacker can arrange `header_deps` such that both the C VM index and the Rust index point to blocks with the same block number (e.g., by padding `header_deps` with a carefully chosen block), the block-number guard passes but the two blocks have different `ar` (accumulation rate) values. Rust then computes `calculate_maximum_withdraw` using the wrong `deposit_ar`, producing an incorrect fee and potentially allowing a larger-than-entitled withdrawal to pass fee validation.

### Likelihood Explanation

Any unprivileged transaction sender can craft a DAO withdrawal transaction with an arbitrary `header_dep_index` value in the witness `input_type` field and an arbitrary `header_deps` list. No special privilege is required. The only constraint is that the transaction must be a valid DAO withdrawal (i.e., the sender must hold a DAO cell). The attack is deterministic and reproducible.

### Recommendation

Align the Rust `DaoCalculator` with the C DAO script by truncating `header_dep_index` to its lowest byte before indexing into `header_deps`:

```rust
// Before (wrong — uses full u64):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
.get(header_dep_index as usize)

// After (correct — mirrors C script's u8 truncation):
let raw = LittleEndian::read_u64(&header_deps_index_data.unwrap());
let header_dep_index = (raw & 0xFF) as usize;
// ...
.get(header_dep_index)
```

Alternatively, if the protocol intends to support indices > 255, the C DAO script must be updated to read the full `u64` and the change must be gated behind a hardfork.

### Proof of Concept

The repository's own test file documents and exercises this exact discrepancy: [1](#0-0) 

The production code that reads the full `u64` index: [2](#0-1) 

The test setup explicitly places the deposit block at position 1 (what C VM resolves via lowest byte of 257) and the withdraw block at position 257 (what Rust resolves via full u64), then asserts Rust rejects the transaction — confirming the divergence between the two implementations: [3](#0-2)

### Citations

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

**File:** util/dao/src/lib.rs (L91-99)
```rust
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
                                        .and_then(|hash| header_deps.get(&hash))
                                        .ok_or(DaoError::InvalidOutPoint)
                                })?;
```
