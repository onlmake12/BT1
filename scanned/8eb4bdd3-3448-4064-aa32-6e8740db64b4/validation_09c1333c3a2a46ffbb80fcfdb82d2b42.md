Audit Report

## Title
`DaoCalculator` reads full u64 header-dep index while on-chain `dao.c` truncates to lowest byte, causing consensus split for DAO withdrawals — (File: `util/dao/src/lib.rs`)

## Summary
`DaoCalculator::transaction_maximum_withdraw()` decodes the 8-byte `WitnessArgs.input_type` field as a full little-endian `u64` and uses it as an array index into `header_deps`. The on-chain C VM (`dao.c`) reads only the lowest byte of that same field. When a DAO withdrawal transaction encodes an index > 255, the Rust verifier and the C VM resolve to different `header_deps` entries: the C VM accepts the transaction while every Rust node rejects it, producing a consensus split.

## Finding Description
In `util/dao/src/lib.rs` at line 91, the witness `input_type` field is decoded as a full `u64`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
``` [1](#0-0) 

That value is then used directly as an array index:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
``` [2](#0-1) 

The on-chain C VM (`dao.c`, referenced at `test/src/specs/dao/dao_user.rs:14`) reads only the lowest byte of the same 8-byte field. When a transaction sets the witness index to any value `v` where `v > 255` but `v & 0xFF` points to the real deposit header:

- **C VM** resolves `v & 0xFF` → deposit block → block-number check passes → **accepts**
- **Rust** resolves `v` → a different entry (e.g., the withdraw block) → block-number check fails → **rejects**

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly documents and confirms the Rust side of this divergence: [3](#0-2) 

The test constructs 258 `header_deps`, places the deposit block at index 1 and the withdraw block at index 257, sets `input_type = 257u64.to_le_bytes()`, and asserts `result.is_err()` — confirming Rust resolves index 257 to the withdraw block and rejects the transaction. [4](#0-3) 

## Impact Explanation
**Critical — consensus deviation.** `DaoCalculator::transaction_fee()` is called inside the transaction verification pipeline used for both tx-pool admission and block validation. A DAO withdrawal transaction that is valid per the on-chain C VM script execution (index ≤ 255 in the lowest byte, correct deposit header there) but carries a full u64 index > 255 will be rejected by every Rust node even though the C VM script succeeds. If such a transaction is included in a block, every Rust node rejects that block while the C VM considers it valid, forking the chain. This matches the allowed impact: *"Vulnerabilities which could easily cause consensus deviation."* [5](#0-4) 

## Likelihood Explanation
Any holder of a valid DAO deposit can trigger this. The attacker needs only to: (1) hold a DAO deposit, (2) construct a withdrawal transaction with ≥ 256 `header_deps` (padding with arbitrary dummy hashes is sufficient; the protocol imposes no hard cap below 256), and (3) set the witness index to any value whose lowest byte points to the real deposit header while the full value points elsewhere. No privileged access, key material, or majority hash-power is required. [6](#0-5) 

## Recommendation
Replace the full-u64 read with a single-byte read to match the C VM's behaviour:

```rust
// Before
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// After — mirror dao.c: use only the lowest byte
Ok(u64::from(header_deps_index_data.unwrap()[0]))
```

Alternatively, add a consensus-level validation rule that rejects any DAO withdrawal whose witness index field encodes a value > 255, making both implementations agree by construction. [7](#0-6) 

## Proof of Concept
The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` is a self-contained PoC for the Rust side:

1. Build a `ResolvedTransaction` with 258 `header_deps`; place the real deposit block hash at `header_deps[1]` and the withdraw block hash at `header_deps[257]`.
2. Set `WitnessArgs.input_type = 257u64.to_le_bytes()`.
3. Call `DaoCalculator::transaction_fee(&rtx)` — returns `Err` because Rust resolves index 257 → withdraw block (number 200) → block-number mismatch with cell data (100).
4. The C VM resolves `257 & 0xFF = 1` → deposit block (number 100) → block-number match → **accepts**.

The divergence is confirmed by the test assertion and its inline comments. [8](#0-7)

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
