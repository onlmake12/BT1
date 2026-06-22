### Title
`DaoCalculator::transaction_maximum_withdraw` Resolves Deposit Header Using Full `u64` Index While On-Chain `dao.c` Uses Only the Lowest Byte, Causing Incorrect Fee Accounting and Tx-Pool/Block Validity Divergence — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` reads the 8-byte little-endian `input_type` witness field as a full `u64` to index into `header_deps` when resolving the deposit header for a NervosDAO withdrawal. The on-chain C VM `dao.c` script reads the same field but uses only the **lowest byte** as the index. When the index value exceeds 255, the two components resolve different deposit headers, causing the Rust fee calculator and the C VM script to operate on different data — a direct analog to the external report's "wrong value used in calculation" vulnerability class.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the `header_deps` index from the witness `input_type` field as a full `u64`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
``` [1](#0-0) 

The on-chain `dao.c` script, however, reads only the **lowest byte** of the same 8-byte little-endian field to determine the `header_deps` index. This is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
// input_type = 257, lowest byte = 1
``` [2](#0-1) 

When a transaction sets `input_type` to any value ≥ 256 (e.g., 257 = `[0x01, 0x01, 0x00, ...]` in little-endian), the Rust calculator uses index 257 while the C VM uses index 1. These resolve to different entries in `header_deps`, so the two components use different deposit headers for the DAO withdrawal calculation.

This `transaction_maximum_withdraw` is called from three production paths:

1. **`FeeCalculator::transaction_fee`** in `ContextualTransactionVerifier::verify` — used for both tx-pool admission and block-level transaction validation. [3](#0-2) 

2. **`check_tx_fee`** in the tx-pool — directly calls `DaoCalculator::transaction_fee` to gate tx-pool admission. [4](#0-3) 

3. **`DaoHeaderVerifier::verify`** — calls `DaoCalculator::dao_field` → `withdrawed_interests` → `transaction_maximum_withdraw` to verify the DAO field embedded in block headers. [5](#0-4) 

---

### Impact Explanation

**Scenario A — Rust accepts, C VM rejects (invalid block production):**

A transaction sender crafts a DAO withdrawal with 258+ `header_deps`:
- `header_deps[1]` = dummy block (not the deposit block)
- `header_deps[257]` = deposit_block (correct deposit block, number matches cell_data)
- witness `input_type` = 257

Rust `DaoCalculator`: index 257 → deposit_block → block number check passes → fee computed, tx admitted to pool and assembled into a block.

C VM `dao.c`: lowest byte = 1 → dummy block → block number check fails → script execution rejects the transaction.

The block is assembled by the miner (Rust says valid) but rejected by all validating nodes (C VM says invalid). This causes **invalid block production** and **wasted miner resources**.

**Scenario B — Rust rejects, C VM accepts (valid tx incorrectly rejected):**

This is the scenario documented by the existing test: with `header_deps[1]` = deposit_block and `header_deps[257]` = withdraw_block, and `input_type` = 257, the Rust calculator resolves the withdraw block as the "deposit" header, fails the block number check, and rejects a transaction that the C VM would accept. [6](#0-5) 

**Scenario C — Incorrect DAO field in block headers:**

Because `DaoHeaderVerifier` uses the same `DaoCalculator`, a block containing a crafted DAO withdrawal transaction would have its DAO field computed with the wrong deposit header, causing the block to be rejected by the Rust verifier even if the C VM accepted the transaction. [7](#0-6) 

---

### Likelihood Explanation

Any unprivileged transaction sender can craft a DAO withdrawal transaction with 258+ `header_deps` and set `input_type` to 257. The CKB protocol does not limit the number of `header_deps` in a transaction beyond the block size limit. The attacker only needs to control the witness and `header_deps` fields of their own transaction — no privileged access, no key compromise, no majority hashpower required.

---

### Recommendation

In `DaoCalculator::transaction_maximum_withdraw`, truncate the extracted index to its lowest byte before using it, to match the C VM `dao.c` behavior:

```rust
// Before:
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// After (match C VM lowest-byte behavior):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()) & 0xFF)
```

Alternatively, explicitly reject any `input_type` value whose upper 7 bytes are non-zero, returning `DaoError::InvalidDaoFormat`, so that the Rust verifier and C VM agree on which transactions are valid. [8](#0-7) 

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` demonstrates Scenario B (Rust rejects what C VM accepts):

```
header_deps[1]   = deposit_block  (block 100)  ← C VM resolves here (lowest byte of 257 = 1)
header_deps[257] = withdraw_block (block 200)  ← Rust resolves here (full u64 = 257)
input_type       = 257
cell_data        = 100 (deposit block number)

Rust:  index 257 → withdraw_block (200) ≠ cell_data (100) → Err ✓ (test asserts this)
C VM:  index   1 → deposit_block  (100) = cell_data (100) → accepts
``` [9](#0-8) 

For Scenario A (Rust accepts, C VM rejects — the more dangerous direction), swap the positions:

```
header_deps[1]   = dummy_block    (any block ≠ deposit)  ← C VM resolves here → fails
header_deps[257] = deposit_block  (block 100)             ← Rust resolves here → passes
input_type       = 257
cell_data        = 100

Rust:  index 257 → deposit_block (100) = cell_data (100) → accepts → tx enters pool/block
C VM:  index   1 → dummy_block         ≠ cell_data (100) → rejects → block invalid
```

This causes the miner's block assembler to produce an invalid block, wasting block space and mining rewards, and causing a consensus split between the block producer and validating nodes.

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

**File:** util/dao/src/tests.rs (L476-537)
```rust
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

**File:** verification/src/transaction_verifier.rs (L265-273)
```rust
    fn transaction_fee(&self) -> Result<Capacity, DaoError> {
        // skip tx fee calculation for cellbase
        if self.transaction.is_cellbase() {
            Ok(Capacity::zero())
        } else {
            DaoCalculator::new(self.consensus.as_ref(), &self.data_loader)
                .transaction_fee(&self.transaction)
        }
    }
```

**File:** tx-pool/src/util.rs (L34-41)
```rust
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
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
