### Title
NervosDAO Withdrawal Witness Index Interpretation Discrepancy Between Rust Verifier and On-Chain C VM Causes Consensus Split - (File: `util/dao/src/lib.rs`)

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the full `u64` value from the `WitnessArgs.input_type` field to index into `header_deps` and resolve the deposit block header. The on-chain C VM dao.c script reads only the **lowest byte** (effectively a `u8`) of the same field. When a DAO withdrawal transaction encodes a witness index greater than 255 (e.g., 257), the C VM resolves `header_deps[1]` (the correct deposit header), while the Rust verifier resolves `header_deps[257]` (a different header). The Rust node then rejects the block containing this transaction as `InvalidDAO`, while the C VM accepts it — causing a consensus split.

### Finding Description

In `DaoCalculator::transaction_maximum_withdraw`, the deposit header is resolved by reading a `u64` index from the witness and using it directly as a `usize` array index:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // full u64 used as index
``` [1](#0-0) 

The on-chain C VM dao.c script (referenced at `dao_user.rs` line 14) reads only the lowest byte of the same 8-byte field, effectively treating the index as a `u8`. This discrepancy is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
// input_type = 257, lowest byte = 1
``` [2](#0-1) 

The `DaoCalculator` is invoked during block validation inside `DaoHeaderVerifier::verify()`, which recomputes the DAO field for every block and rejects the block if the computed value does not match the header:

```rust
let dao = DaoCalculator::new(...)
    .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
    ...?;
if dao != self.header.dao() {
    return Err((BlockErrorKind::InvalidDAO).into());
}
``` [3](#0-2) 

When `transaction_maximum_withdraw` resolves `header_deps[257]` (a different canonical block with a different block number than the deposited block number stored in cell data), the block number check at line 105 fires:

```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
``` [4](#0-3) 

This error propagates through `withdrawed_interests` → `dao_field_with_current_epoch` → `dao_field` → `DaoHeaderVerifier::verify()`, causing the Rust node to reject the block as `InvalidDAO`. The C VM, however, correctly resolved `header_deps[1]` (the deposit header) and accepted the transaction.

### Impact Explanation

A DAO withdrawal transaction with 258+ `header_deps` and a witness index of 257 (lowest byte = 1) is valid per the on-chain C VM but is rejected by the Rust node's `DaoCalculator`. If a miner includes such a transaction in a block, the block is valid per consensus (C VM accepted it) but the Rust node rejects it as `InvalidDAO`. This causes the Rust node to fork from the canonical chain — a consensus split. Funds are not directly stolen, but the Rust node becomes unable to follow the canonical chain, disrupting node operation and potentially enabling double-spend attacks against services relying on the split node.

### Likelihood Explanation

The attacker must be a NervosDAO depositor (unprivileged) and must either be a miner or convince a miner to include the crafted transaction. Constructing a withdrawal with 258 `header_deps` is technically straightforward — 258 × 32 bytes = ~8 KB, well within `max_block_bytes`. No privileged access, leaked keys, or majority hashpower is required. The discrepancy is already documented in the codebase test, confirming it is a known behavioral difference between the two execution environments.

### Recommendation

In `DaoCalculator::transaction_maximum_withdraw`, truncate the witness index to a `u8` before using it as a `header_deps` array index, to match the C VM dao.c behavior:

```rust
// Change:
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// To:
Ok(u64::from(header_deps_index_data.unwrap()[0]))  // lowest byte only, matching C VM
```

Alternatively, add a consensus rule rejecting DAO withdrawal transactions whose witness index exceeds 255, so both the Rust verifier and the C VM agree on the valid range.

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` already demonstrates the discrepancy:

1. Construct a DAO withdrawal transaction with 258 `header_deps`.
2. Place the correct deposit block at `header_deps[1]` and the withdraw block at `header_deps[257]`.
3. Set `WitnessArgs.input_type` = `257u64` (little-endian 8 bytes).
4. C VM reads lowest byte = 1 → resolves `header_deps[1]` = deposit block → **accepts** the transaction.
5. Rust reads full u64 = 257 → resolves `header_deps[257]` = withdraw block (number 200) → block number ≠ deposited block number (100) → `Err(DaoError::InvalidOutPoint)`.
6. A miner includes this transaction in a block; the block is valid per C VM consensus.
7. The Rust node calls `DaoHeaderVerifier::verify()`, which calls `DaoCalculator::dao_field()`, which propagates the error → block rejected as `InvalidDAO`.
8. Rust node forks from the canonical chain. [5](#0-4) [6](#0-5)

### Citations

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

**File:** util/dao/src/lib.rs (L105-107)
```rust
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
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
