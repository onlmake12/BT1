### Title
`DaoCalculator` reads full u64 header-dep index while on-chain C VM truncates to lowest byte, causing consensus split for DAO withdrawals — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw()` interprets the 8-byte witness `input_type` field as a full `u64` index into `header_deps`. The on-chain C VM (`dao.c`) reads only the **lowest byte** of that same field. When a DAO withdrawal transaction encodes an index > 255, the Rust verifier and the C VM resolve to different `header_deps` entries, producing a consensus split: the C VM accepts the transaction while the Rust node rejects it.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw()` decodes the witness `input_type` field as a little-endian `u64` and uses the full value as an array index:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// …
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // full u64 used here
``` [1](#0-0) 

The on-chain C VM (`dao.c`, referenced at `test/src/specs/dao/dao_user.rs:14`) reads only the **lowest byte** of the same 8-byte field. The production test `check_dao_withdraw_header_dep_index_exceeds_u8` explicitly documents this divergence:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
``` [2](#0-1) 

The test constructs a transaction with 258 `header_deps`, places the correct deposit block at index 1 and the withdraw block at index 257, then sets the witness index to `257u64`. The C VM resolves `257 & 0xFF = 1` → deposit block (block 100) → block-number check passes → **C VM accepts**. The Rust verifier resolves `257` → withdraw block (block 200) → block-number check fails → **Rust rejects**. [3](#0-2) 

---

### Impact Explanation

`DaoCalculator::transaction_fee()` is invoked inside `verification/src/transaction_verifier.rs` as part of the transaction-verification pipeline used for both tx-pool admission and block validation. A DAO withdrawal transaction that is **valid per the on-chain C VM** (index ≤ 255 in the lowest byte, correct deposit header there) but carries a full u64 index > 255 will be:

1. **Rejected from the tx-pool** — the depositor cannot withdraw their DAO funds through any standard Rust node.
2. **Cause a chain split** — if such a transaction is relayed by a non-standard path and included in a block, every Rust node will reject that block even though the C VM script execution succeeds, forking the chain. [4](#0-3) 

---

### Likelihood Explanation

A transaction sender who controls a DAO deposit can craft a withdrawal transaction with ≥ 256 `header_deps` (padding with dummy hashes is sufficient; the protocol imposes no hard cap below 256 on `header_deps` count). Setting the witness index to any value whose lowest byte points to the real deposit header while the full value points elsewhere is straightforward. No privileged access, key material, or majority hash-power is required — only a valid DAO deposit and the ability to submit a transaction. [5](#0-4) 

---

### Recommendation

Replace the full-u64 read with a single-byte read to match the C VM's behaviour:

```rust
// Before
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// After — mirror dao.c: use only the lowest byte
Ok(u64::from(header_deps_index_data.unwrap()[0]))
```

Alternatively, add a consensus-level rule that rejects any DAO withdrawal whose witness index exceeds 255, making the two implementations agree by construction. [6](#0-5) 

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` is a self-contained PoC:

1. Build a `ResolvedTransaction` with 258 `header_deps`.
2. Place the real deposit block hash at `header_deps[1]` and the withdraw block hash at `header_deps[257]`.
3. Set `WitnessArgs.input_type = 257u64.to_le_bytes()`.
4. Call `DaoCalculator::transaction_fee(&rtx)` — it returns `Err` (Rust resolves index 257 → withdraw block → block-number mismatch).
5. The C VM would resolve `257 & 0xFF = 1` → deposit block → block-number match → **accept**.

The divergence is confirmed by the test assertion and its inline comments. [5](#0-4)

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
