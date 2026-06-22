### Title
DAO Phase 2 Withdrawal Permanently Rejected by Rust Fee Calculator When Deposit Block `header_dep` Index Exceeds 255 — (`util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator` reads the full 8-byte `u64` `header_dep_index` from the witness `input_type` field, while the C VM DAO script reads only the **lowest byte** (`u8`). When the deposit block's position in `header_deps` exceeds 255, the two implementations resolve different blocks, causing the Rust fee calculator to reject a transaction that the DAO script (C VM) would accept. A user whose Phase 2 withdrawal transaction has a deposit block at index > 255 in `header_deps` will have their transaction permanently rejected by the node, leaving funds stuck in the prepare cell.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` parses the `header_dep_index` from the witness as a full `u64`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then uses this value directly to index into `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
```

The C VM DAO script, however, reads only the **lowest byte** of the 8-byte `input_type` field. This discrepancy is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

When `input_type` = `257` (little-endian bytes: `0x01 0x01 0x00 0x00 0x00 0x00 0x00 0x00`):
- **C VM** reads lowest byte = `0x01` = 1 → uses `header_deps[1]` (the deposit block, block number 100 = cell data → script passes)
- **Rust** reads full `u64` = 257 → uses `header_deps[257]` (the withdraw block, block number 200 ≠ cell data 100 → `InvalidOutPoint` error)

The test asserts `result.is_err()`, confirming Rust rejects a transaction that C VM would accept. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

**Impact: High — user funds stuck in prepare cell**

The two-phase DAO withdrawal process is:
1. **Phase 1 (prepare):** User locks funds in a prepare cell; cell data is set to the deposit block number.
2. **Phase 2 (withdraw):** User submits a withdrawal transaction referencing the deposit block via `header_deps` and the witness index.

If the deposit block's position in `header_deps` is > 255 (e.g., index 256 or 257), the Rust fee calculator resolves a different block than C VM. Rust returns `DaoError::InvalidOutPoint` (block number mismatch), causing the node to reject the transaction at the tx-pool admission stage. The user cannot complete Phase 2. Their CKB capacity is locked in the prepare cell with no valid path to withdrawal through this node.

This is directly analogous to the LidoVault bug: a withdrawal request is initiated in one state (Phase 1), and when the user attempts to finalize it (Phase 2), the node's accounting logic uses a different reference point than the authoritative script, causing the finalization to fail and funds to be stuck. [3](#0-2) 

---

### Likelihood Explanation

**Likelihood: Medium**

The discrepancy is triggered when the deposit block's index in `header_deps` exceeds 255. This occurs in two realistic scenarios:

1. **Legitimate user with many DAO cells:** A user withdrawing many DAO cells in a single Phase 2 transaction includes both deposit and prepare block hashes in `header_deps`. With > 127 DAO cells in one transaction, some deposit block indices will exceed 255. The `header_deps` list is deduplicated via `HashSet` (order is non-deterministic), so the index assigned to any given deposit block is unpredictable.

2. **Attacker-crafted transaction:** Any unprivileged tx-pool submitter or RPC caller can craft a Phase 2 withdrawal with `input_type` = 257 and 258 `header_deps`, placing the deposit block at index 1 (C VM resolves it correctly) and a different block at index 257 (Rust resolves it incorrectly). This causes Rust to reject a transaction that C VM would accept. [4](#0-3) [5](#0-4) 

---

### Recommendation

Align the Rust `DaoCalculator` with the C VM DAO script by reading only the lowest byte of the `input_type` field as the `header_dep_index`:

```rust
// Current (reads full u64):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// Fixed (reads only lowest byte, matching C VM behavior):
Ok(u64::from(header_deps_index_data.unwrap()[0]))
```

Alternatively, add a validation step that rejects any `header_dep_index` > 255 with a clear error, preventing the silent divergence between Rust and C VM. The two implementations must agree on the interpretation of the index field. [6](#0-5) 

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the discrepancy:

1. A DAO withdrawal transaction is constructed with 258 `header_deps`.
2. `header_deps[1]` = deposit block (block number 100, matching cell data).
3. `header_deps[257]` = withdraw block (block number 200, not matching cell data).
4. `input_type` = `257u64` (lowest byte = 1).
5. **C VM** resolves index 1 → deposit block (number 100 = cell data) → **would accept**.
6. **Rust** resolves index 257 → withdraw block (number 200 ≠ cell data 100) → **returns `Err`**.
7. The test asserts `result.is_err()`, confirming Rust rejects a transaction C VM would accept.

A user with a legitimate DAO withdrawal whose deposit block lands at index > 255 in `header_deps` (due to non-deterministic `HashSet` ordering during transaction construction) will have their Phase 2 transaction rejected by every node, with no recourse other than restructuring the transaction to reduce `header_deps` count — which may not always be possible if the user has many DAO cells. [7](#0-6) [8](#0-7)

### Citations

**File:** util/dao/src/lib.rs (L73-99)
```rust
                            let deposit_header_hash = rtx
                                .transaction
                                .witnesses()
                                .get(i)
                                .ok_or(DaoError::InvalidOutPoint)
                                .and_then(|witness_data| {
                                    // dao contract stores header deps index as u64 in the input_type field of WitnessArgs
                                    let witness =
                                        WitnessArgs::from_slice(&Into::<Bytes>::into(witness_data))
                                            .map_err(|_| DaoError::InvalidDaoFormat)?;
                                    let header_deps_index_data: Option<Bytes> =
                                        witness.input_type().to_opt().map(|witness| witness.into());
                                    if header_deps_index_data.is_none()
                                        || header_deps_index_data.clone().map(|data| data.len())
                                            != Some(8)
                                    {
                                        return Err(DaoError::InvalidDaoFormat);
                                    }
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

**File:** test/src/specs/dao/dao_user.rs (L155-161)
```rust
        let header_deps = deposit_utxo_headers
            .iter()
            .chain(prepare_utxo_headers.iter())
            .map(|(_, header)| header.hash())
            .collect::<HashSet<_>>()
            .into_iter()
            .collect::<Vec<_>>();
```
