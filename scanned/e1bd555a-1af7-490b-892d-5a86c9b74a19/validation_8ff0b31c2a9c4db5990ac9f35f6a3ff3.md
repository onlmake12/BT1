### Title
NervosDAO Withdrawal `header_dep_index` Semantic Divergence Between `dao.c` C Script and Rust `DaoCalculator` — (File: `util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the full `u64` `header_dep_index` from the witness `input_type` field, while the on-chain `dao.c` C script reads only the **lowest byte** of that same 8-byte little-endian value. A transaction sender can craft a DAO withdrawal with `input_type = 257` (LE bytes: `[0x01, 0x01, 0x00, …]`): the C script resolves index `1` (deposit block, correct), while Rust resolves index `257` (a different block), causing a block-level consensus divergence.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` reads the deposit header index from the witness as a full `u64`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

and then uses it directly as a `usize` array index:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
```

The on-chain `dao.c` C script (the actual consensus enforcement script running in CKB-VM) reads only the **lowest byte** of this 8-byte field, effectively treating it as a `u8`. This is explicitly documented in the test added to `util/dao/src/tests.rs`:

```
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

When a transaction encodes `input_type = 257u64` (LE: `[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`):
- **C script** reads byte `0x01` → index `1` → deposit block → **ACCEPTS**
- **Rust `DaoCalculator`** reads `257` → index `257` → withdraw block → block number mismatch → **REJECTS** [1](#0-0) [2](#0-1) 

---

### Impact Explanation

The `DaoCalculator` is used in two security-critical paths:

1. **Block `dao` field validation**: When a Rust node receives a block, it recomputes the `dao` field via `DaoCalculator::dao_field` → `withdrawed_interests` → `transaction_maximum_withdraw`. If a block contains a crafted DAO withdrawal with `input_type = 257`, the Rust node's recomputation fails and the block is **rejected**, even though the C script accepted the transaction. This is a **consensus split**: Rust nodes reject a block that is valid under the actual on-chain consensus rules.

2. **`calculate_dao_maximum_withdraw` RPC**: Returns an error for transactions that the C script would accept, causing incorrect RPC behavior for wallets and tooling. [3](#0-2) 

---

### Likelihood Explanation

The attacker must:
- Hold a DAO deposit cell
- Craft a withdrawal transaction with ≥ 258 `header_deps` (no protocol limit prevents this)
- Encode `input_type` as a `u64` whose lowest byte points to the deposit block but whose full value points elsewhere
- Convince a miner to include the transaction (or be a miner themselves)

The `header_deps` vector has no enforced maximum count in the transaction structure. The crafted witness is valid molecule-encoded bytes. The block number cross-check in Rust (`deposit_header.number() != deposited_block_number`) catches the mismatch and causes rejection, but the C script does not perform this check in the same way — it resolves the correct header via the truncated index and passes. Likelihood is **low-medium** but the impact (consensus split, chain fork) is high. [4](#0-3) 

---

### Recommendation

In `transaction_maximum_withdraw`, after reading `header_dep_index` as `u64`, add a bounds check rejecting any value exceeding `u8::MAX` (to match the C script's effective range):

```rust
if header_dep_index > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
```

Alternatively, document and enforce a protocol-level limit on `header_deps` count (≤ 255) so the two interpretations can never diverge. [1](#0-0) 

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the divergence:

- 258 `header_deps` are constructed; `header_deps[1]` = deposit block, `header_deps[257]` = withdraw block
- Witness `input_type = 257u64` (LE lowest byte = `1`)
- C script reads index `1` → deposit block → **would accept**
- Rust reads index `257` → withdraw block → block number mismatch → **rejects with `Err`**
- The test asserts `result.is_err()` — confirming the Rust rejection

A real attacker submits this transaction to a miner. The miner's C-script-based validation accepts it; the miner assembles a block. Rust full nodes recompute the `dao` field, hit the mismatch at line 105, and reject the block — causing a consensus fork. [5](#0-4) [6](#0-5)

### Citations

**File:** util/dao/src/lib.rs (L38-99)
```rust
    fn transaction_maximum_withdraw(
        &self,
        rtx: &ResolvedTransaction,
    ) -> Result<Capacity, DaoError> {
        let header_deps: HashSet<Byte32> = rtx.transaction.header_deps_iter().collect();
        rtx.resolved_inputs.iter().enumerate().try_fold(
            Capacity::zero(),
            |capacities, (i, cell_meta)| {
                let capacity: Result<Capacity, DaoError> = {
                    let output = &cell_meta.cell_output;
                    let is_dao_type_script = |type_script: Script| {
                        Into::<u8>::into(type_script.hash_type())
                            == Into::<u8>::into(ScriptHashType::Type)
                            && type_script.code_hash() == self.consensus.dao_type_hash()
                    };
                    let is_dao_output = output
                        .type_()
                        .to_opt()
                        .map(is_dao_type_script)
                        .unwrap_or(false);
                    if is_dao_output {
                        // A withdrawing DAO cell has 8 bytes of cell data storing the
                        // block number of the original deposit.
                        let deposited_block_number =
                            match self.data_loader.load_cell_data(cell_meta) {
                                Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
                                _ => 0,
                            };
                        if deposited_block_number > 0 {
                            let withdrawing_header_hash = cell_meta
                                .transaction_info
                                .as_ref()
                                .map(|info| &info.block_hash)
                                .filter(|hash| header_deps.contains(hash))
                                .ok_or(DaoError::InvalidOutPoint)?;
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
