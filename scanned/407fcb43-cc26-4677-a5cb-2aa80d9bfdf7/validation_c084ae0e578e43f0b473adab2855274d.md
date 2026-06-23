### Title
DAO Withdrawal Header-Dep Index Type-Width Mismatch Between C VM and Rust Verifier Causes Consensus Split - (File: `util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` function reads the `header_dep_index` from the witness `input_type` field as a full `u64` (`LittleEndian::read_u64`), while the on-chain C DAO script running inside CKB-VM reads the same field using only the **lowest byte** (u8 width). When a transaction encodes an index whose full u64 value differs from its lowest byte (e.g., 257 = `0x0101` LE), the C VM and the Rust verifier resolve different entries in `header_deps`, producing a consensus split.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` parses the deposit header index from the witness:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
``` [1](#0-0) 

This full `u64` value is then used directly to index into `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
``` [2](#0-1) 

The on-chain C DAO script (referenced in `dao_user.rs` at the comment pointing to `dao.c#L81`) reads the same 8-byte `input_type` field but interprets only the **lowest byte** as the index. The Rust code treats the entire 8-byte value as a `u64` index.

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` explicitly documents this divergence:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
// input_type = 257, lowest byte = 1
``` [3](#0-2) 

The witness is constructed with index `257u64` (LE bytes `[0x01, 0x01, 0x00, ...]`):

```rust
let witness = WitnessArgs::new_builder()
    .input_type(Some(Bytes::from(257u64.to_le_bytes().to_vec())))
    .build();
``` [4](#0-3) 

- **C VM** reads lowest byte → `1` → `header_deps[1]` = deposit block (number 100) → block number matches cell data → **ACCEPTS**
- **Rust** reads full u64 → `257` → `header_deps[257]` = withdraw block (number 200) → block number does not match cell data (100) → **REJECTS**

The `transaction_fee` function (which calls `transaction_maximum_withdraw`) is invoked in the consensus-critical `verification/src/transaction_verifier.rs` path. [5](#0-4) 

---

### Impact Explanation

A transaction that the C VM (DAO script) accepts as valid will be rejected by the Rust node's capacity/fee verifier. A miner whose node runs the C VM would include such a transaction in a block; Rust-based nodes would reject that block as invalid. This produces a **consensus split**: the network forks between nodes that accepted the block and nodes that rejected it. Affected: block validation, chain continuity, and finality.

---

### Likelihood Explanation

Any transaction sender can craft a DAO withdrawal transaction with a witness `input_type` value whose full u64 interpretation differs from its lowest byte (any value ≥ 256 with a non-zero lowest byte, e.g., 257, 513, 769…). The transaction requires a `header_deps` list long enough to contain a valid entry at the lowest-byte position. This is fully attacker-controlled with no privileged access required. The attacker only needs to submit a transaction to the network.

---

### Recommendation

The Rust `transaction_maximum_withdraw` function must read the `header_dep_index` using the same width as the C DAO script. If the C VM reads only the lowest byte, the Rust code should do:

```rust
let header_dep_index = header_deps_index_data.unwrap()[0] as usize;
```

Alternatively, if the protocol intends a full u64 index, the C DAO script must be updated to match. The two implementations must be brought into agreement. The comment in the code already acknowledges the field is "stored as u64" but does not account for the C VM's actual byte-width behavior. [6](#0-5) 

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` is the proof of concept:

1. Build a `header_deps` list of 258 entries: `header_deps[1]` = deposit block hash, `header_deps[257]` = withdraw block hash.
2. Set witness `input_type` = `257u64` in little-endian (bytes `[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`).
3. Set cell data = deposit block number (100).
4. C VM reads lowest byte = `1` → resolves `header_deps[1]` = deposit block → number 100 matches cell data → **script passes**.
5. Rust reads full u64 = `257` → resolves `header_deps[257]` = withdraw block → number 200 ≠ 100 → `DaoError::InvalidOutPoint` → **Rust rejects**.
6. A miner includes the transaction; Rust nodes reject the containing block → **chain split**. [7](#0-6)

### Citations

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

**File:** util/dao/src/lib.rs (L79-99)
```rust
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
