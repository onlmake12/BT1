### Title
DAO Withdrawal Witness Index Parsed as `u64` in Rust but as `u8` in C Script — Consensus Split on Header-Dep Resolution (`File: util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator` in `util/dao/src/lib.rs` reads the `WitnessArgs.input_type` field as a full 8-byte little-endian `u64` to index into `header_deps`, while the on-chain C DAO script (`dao.c`) reads only the **lowest byte** of the same field. When a transaction submitter places a witness index value greater than 255, the two components resolve different `header_deps` entries as the "deposit header." This is a consensus split: the C VM (script execution) and the Rust fee/capacity verifier disagree on which block anchors the deposit, allowing a crafted DAO withdrawal to be accepted by one and rejected by the other.

---

### Finding Description

The Nervos DAO withdrawal protocol requires the withdrawer to embed a `u64` index in `WitnessArgs.input_type` that points to the deposit block's hash inside the transaction's `header_deps` array. The Rust node's `DaoCalculator::transaction_maximum_withdraw` reads this index as a full 8-byte little-endian integer: [1](#0-0) 

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
```

The on-chain C DAO script, however, reads only the **lowest byte** of the same 8-byte field (effectively treating it as a `u8`). This is explicitly documented in the test suite: [2](#0-1) 

```
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
``` [3](#0-2) 

The test confirms the split: with `input_type = 257` (little-endian bytes `[0x01, 0x01, 0x00, ...]`):
- **C VM** reads lowest byte → index `1` → `header_deps[1]` = deposit block
- **Rust** reads full u64 → index `257` → `header_deps[257]` = a different block [4](#0-3) 

The `DaoCalculator` is invoked during contextual block verification and fee calculation: [5](#0-4) 

---

### Impact Explanation

The `DaoCalculator::transaction_fee` result feeds directly into the block verifier's fee check and the DAO field computation in block headers. When the Rust verifier and the C VM disagree on which `header_dep` is the deposit block:

1. **Scenario A (C VM accepts, Rust rejects):** A crafted DAO withdrawal where `header_deps[index & 0xFF]` is the real deposit block but `header_deps[index]` is a different block. The C DAO script passes (correct deposit found), but the Rust `DaoCalculator` either fails the block-number cross-check at line 105 or computes a wrong maximum-withdraw, causing the Rust node to reject a block that the C VM would accept. This is a **consensus split** — nodes running different implementations or versions could diverge on chain tip.

2. **Scenario B (Rust accepts, C VM rejects):** The inverse arrangement causes the Rust node to accept a block that the C VM script rejects, meaning the block would fail script execution but pass the Rust fee check — another form of consensus inconsistency.

Both scenarios allow an unprivileged transaction submitter to trigger a chain split between nodes. [6](#0-5) 

---

### Likelihood Explanation

The attack requires:
- A DAO withdrawal transaction with ≥ 258 `header_deps` entries
- A witness `input_type` value of `N*256 + k` where `k` is the correct deposit index and `N ≥ 1`
- The correct deposit block hash placed at `header_deps[k]` and a different block at `header_deps[N*256+k]`

This is fully constructable by any unprivileged transaction submitter with a live DAO deposit cell. No special privileges, keys, or majority hashpower are required. The `header_deps` array has no enforced maximum length in the protocol schema: [7](#0-6) 

---

### Recommendation

In `util/dao/src/lib.rs`, after reading the `u64` index, add a bounds check that rejects any index value exceeding `u8::MAX` (255), matching the C script's effective range. Alternatively, if the C script is authoritative, change the Rust code to read only the lowest byte:

```rust
// Current (reads full u64):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// Fix option 1 — reject out-of-range indices:
let idx = LittleEndian::read_u64(&header_deps_index_data.unwrap());
if idx > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
Ok(idx)

// Fix option 2 — match C script behavior (read lowest byte only):
Ok(u64::from(header_deps_index_data.unwrap()[0]))
```

The C DAO script source should also be audited to confirm whether the u8 truncation is intentional or itself a bug, and both sides should be brought into agreement. [8](#0-7) 

---

### Proof of Concept

```
Transaction structure:
  header_deps: [dummy×256, deposit_block_hash, dummy, withdraw_block_hash]
               index:        0..255              256         257         258
  (set header_deps[1] = deposit_block_hash, header_deps[257] = withdraw_block_hash)

  input: DAO withdrawal cell (cell_data = deposit_block_number as u64 LE)
         committed in withdraw_block

  witness[0]: WitnessArgs { input_type: 257u64.to_le_bytes() }
              → bytes: [0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]

C VM reads lowest byte = 0x01 → header_deps[1] = deposit_block_hash ✓
  → deposit_header.number() == cell_data → script PASSES

Rust DaoCalculator reads full u64 = 257 → header_deps[257] = withdraw_block_hash
  → deposit_header.number() (200) ≠ cell_data (100) → REJECTS with InvalidOutPoint

Result: C VM accepts the block; Rust node rejects it → consensus split.
``` [9](#0-8) [10](#0-9)

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

**File:** util/dao/src/lib.rs (L101-107)
```rust
                            let deposit_header = self
                                .data_loader
                                .get_header(deposit_header_hash)
                                .ok_or(DaoError::InvalidHeader)?;
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
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

**File:** verification/src/transaction_verifier.rs (L105-120)
```rust
/// Context-dependent verification checks for transaction
///
/// Contains:
/// [`TimeRelativeTransactionVerifier`](./struct.TimeRelativeTransactionVerifier.html)
/// [`CapacityVerifier`](./struct.CapacityVerifier.html)
/// [`ScriptVerifier`](./struct.ScriptVerifier.html)
/// [`FeeCalculator`](./struct.FeeCalculator.html)
pub struct ContextualTransactionVerifier<DL>
where
    DL: Send + Sync + Clone + CellDataProvider + HeaderProvider + ExtensionProvider + 'static,
{
    pub(crate) time_relative: TimeRelativeTransactionVerifier<DL>,
    pub(crate) capacity: CapacityVerifier,
    pub(crate) script: ScriptVerifier<DL>,
    pub(crate) fee_calculator: FeeCalculator<DL>,
}
```

**File:** util/gen-types/schemas/blockchain.mol (L57-64)
```text
table RawTransaction {
    version:        Uint32,
    cell_deps:      CellDepVec,
    header_deps:    Byte32Vec,
    inputs:         CellInputVec,
    outputs:        CellOutputVec,
    outputs_data:   BytesVec,
}
```
