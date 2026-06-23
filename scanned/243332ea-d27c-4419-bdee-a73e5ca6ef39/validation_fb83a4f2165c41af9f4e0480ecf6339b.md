### Title
DAO Withdrawal Interest Accounting Mismatch: `DaoCalculator` Reads Full `u64` Index While On-Chain `dao.c` Reads Only Lowest Byte — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` resolves the deposit header by reading the full `u64` `header_dep_index` from the witness `input_type` field. The on-chain C DAO script (`dao.c`) reads only the **lowest byte** (u8) of the same field. When `header_dep_index > 255`, the two implementations resolve to different entries in `header_deps`, producing a different deposit header and therefore a different interest calculation. This discrepancy propagates into `DaoHeaderVerifier`, which uses `DaoCalculator::dao_field()` to verify the `dao` field embedded in every block header. A block containing a DAO withdrawal transaction with `header_dep_index > 255` will be rejected by the Rust node's `DaoHeaderVerifier` even though the on-chain C script accepts it, creating a consensus-level accounting inconsistency.

---

### Finding Description

**Root cause — index width mismatch in `DaoCalculator`**

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` reads the full 8-byte little-endian integer from the witness and uses it directly as a `usize` index into `header_deps`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)   // full u64 used as index
        .and_then(|hash| header_deps.get(&hash))
        .ok_or(DaoError::InvalidOutPoint)
})?;
```

The on-chain `dao.c` script reads only the lowest byte of the same 8-byte field (equivalent to `index & 0xFF`). For `header_dep_index = 257` (little-endian bytes `[0x01, 0x01, 0, 0, 0, 0, 0, 0]`):

- **Rust** resolves `header_deps[257]`
- **C script** resolves `header_deps[1]` (lowest byte = `0x01`)

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` explicitly documents this divergence:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
// Rust resolves index 257 → withdraw block (number 200), but cell data
// says deposited at block 100. Block number check catches the mismatch.
assert!(result.is_err(), "expected Err, got {result:?}");
```

**How the mismatch propagates to block rejection**

`DaoHeaderVerifier::verify()` in `verification/contextual/src/contextual_block_verifier.rs` calls `DaoCalculator::dao_field()` to recompute the `dao` field for every block being verified:

```rust
pub fn verify(&self) -> Result<(), Error> {
    let dao = DaoCalculator::new(
        &self.context.consensus,
        &self.context.store.borrow_as_data_loader(),
    )
    .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
    .map_err(|e| { ... e })?;          // error propagated → block rejected

    if dao != self.header.dao() {
        return Err((BlockErrorKind::InvalidDAO).into());
    }
    Ok(())
}
```

`dao_field()` internally calls `withdrawed_interests()` → `transaction_maximum_withdraw()`. When a DAO withdrawal transaction with `header_dep_index > 255` is present, `transaction_maximum_withdraw` resolves the wrong deposit header, finds a block-number mismatch against the cell data, and returns `DaoError::InvalidOutPoint`. This error propagates through `dao_field()` and causes `DaoHeaderVerifier` to reject the block.

`DaoHeaderVerifier` is invoked unconditionally (unless `switch.disable_daoheader()`) inside `ContextualBlockVerifier::verify()`, which is the main block-acceptance path in `chain/src/verify.rs`.

**The accounting mismatch**

The `dao` field in every block header encodes four accumulators: `ar`, `C`, `S`, `U`. The `S` accumulator tracks total NervosDAO secondary issuance minus `withdrawed_interests`. When the Rust node and the C script disagree on which deposit header to use, they compute different `withdrawed_interests`, producing different `S` values. The block header's `dao` field is computed by the miner using the C script's logic; the Rust node recomputes it using `DaoCalculator` with the wrong index resolution. The two values diverge, and the Rust node rejects the block with `BlockErrorKind::InvalidDAO`.

---

### Impact Explanation

A malicious miner can craft a DAO withdrawal transaction with `header_dep_index = 257` (or any value `> 255`), placing the correct deposit block at `header_deps[1]` and an unrelated block at `header_deps[257]`. The on-chain C script accepts the transaction (resolves index 1 → correct deposit block). The Rust node's `DaoCalculator` resolves index 257 → wrong block → block-number check fails → `DaoHeaderVerifier` rejects the entire block with `InvalidDAO`.

This creates a consensus inconsistency **within the Rust node itself**: CKB-VM script execution (running `dao.c`) accepts the transaction, while `DaoHeaderVerifier` (running `DaoCalculator`) rejects the block. Any alternative CKB implementation that faithfully follows the C script's u8-truncation behavior would accept the block, causing a chain split with Rust nodes.

Even without alternative implementations, the miner's block is permanently invalid from the Rust node's perspective despite being script-valid, which constitutes a liveness/consensus correctness bug.

---

### Likelihood Explanation

Exploitation requires a miner (or collusion with one) to deliberately craft a DAO withdrawal transaction with `header_dep_index > 255` and assemble a block outside the Rust tx-pool (which also rejects such transactions via `check_tx_fee` → `DaoCalculator::transaction_fee`). This is a non-trivial but realistic capability for a motivated attacker with mining resources. The discrepancy is already documented in the test suite, indicating the developers are aware of the behavioral difference but have not closed the consensus gap.

---

### Recommendation

Fix `DaoCalculator::transaction_maximum_withdraw` in `util/dao/src/lib.rs` to truncate `header_dep_index` to its lowest byte before indexing into `header_deps`, matching the on-chain C script's behavior:

```rust
// Before:
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// After (truncate to u8 to match dao.c):
Ok(u64::from(LittleEndian::read_u64(&header_deps_index_data.unwrap()) as u8))
```

Alternatively, fix the on-chain `dao.c` script to read the full `u64` index. Whichever direction is chosen, both components must agree on the same index resolution to eliminate the accounting mismatch.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the discrepancy: [1](#0-0) 

The Rust `DaoCalculator` reads the full `u64` index here: [2](#0-1) 

`DaoHeaderVerifier` uses `DaoCalculator::dao_field()` during block verification, propagating the error: [3](#0-2) 

`DaoHeaderVerifier` is called unconditionally in the main block-acceptance path: [4](#0-3)

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

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-320)
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
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L670-672)
```rust
        if !self.switch.disable_daoheader() {
            DaoHeaderVerifier::new(&self.context, resolved, &parent, &block.header()).verify()?;
        }
```
