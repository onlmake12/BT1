### Title
DAO Withdrawal `header_dep_index` Width Mismatch: Rust Reads Full `u64`, On-Chain C VM Reads Only Lowest Byte (`u8`) — (File: `util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator` reads the `header_dep_index` from a DAO withdrawal witness as a full 8-byte `u64`, while the on-chain C VM (`dao.c`) reads only the lowest byte (`u8`). When a transaction sender supplies a witness index whose value exceeds 255, the Rust off-chain code resolves a **different** `header_dep` entry than the on-chain script, causing the Rust fee calculator and the `calculate_dao_maximum_withdraw` RPC to operate on an unintended block's accumulation-rate (`ar`) data. The block-number guard that is meant to catch this is bypassable using a fork block at the same height.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit-block index from the witness `input_type` field and immediately uses it as a full `u64` to index into `header_deps`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

followed by:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // full u64 used as index
``` [1](#0-0) 

The on-chain `dao.c` script, however, reads only the **lowest byte** of the same 8-byte field, treating it as a `u8`. This is explicitly documented in the repository's own test:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
// input_type = 257, lowest byte = 1
``` [2](#0-1) 

The only guard against this divergence is a block-number equality check:

```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
``` [3](#0-2) 

This guard fails when the attacker supplies a fork block at the **same height** as the real deposit block at the high index position, because the block numbers match even though the blocks (and their `ar` values) differ.

The vulnerability class is directly analogous to the external report: an incorrect count/bound is used when loading structured data, causing the code to operate on an unintended element. Here, instead of a loop running `2×N` iterations and picking up arbitrary proof data, the index is widened from `u8` to `u64`, causing the Rust code to pick up an arbitrary `header_dep` entry beyond position 255.

---

### Impact Explanation

A transaction sender who submits a DAO withdrawal transaction with ≥ 258 `header_deps` and a witness `input_type` index whose value is `> 255` (e.g., `257 = 0x0101`, lowest byte `0x01`) causes:

1. **Tx-pool fee miscalculation**: `DaoCalculator::transaction_fee` computes `maximum_withdraw` using the wrong block's `ar`. If the attacker places a fork block with a higher `ar` at the high index, the Rust code overestimates the maximum withdrawal, underestimates the fee, and may admit a transaction the C VM will reject on-chain — wasting block space and miner resources.

2. **`calculate_dao_maximum_withdraw` RPC returns wrong value**: Any RPC caller querying this endpoint for a crafted withdrawal transaction receives an incorrect capacity figure, misleading wallets and tooling.

3. **Bypass of the block-number guard**: If the attacker places a fork block at the same height as the real deposit block at `header_deps[257]`, the guard `deposit_header.number() != deposited_block_number` passes, and the Rust code silently uses the wrong block's `ar` for all downstream calculations. [4](#0-3) 

---

### Likelihood Explanation

- **Entry path is fully unprivileged**: any transaction sender or RPC caller can trigger this by submitting a DAO withdrawal transaction with ≥ 258 `header_deps` and a witness index `> 255`. No special role or key is required.
- **258 `header_deps` is unusual but not protocol-prohibited**: CKB imposes no hard cap on `header_deps` count below the block-size limit.
- **Fork blocks at the same height exist on any live chain** after any natural reorganization; an attacker can reference one they observed.
- The block-number guard is the only mitigation and is bypassable as described above.

---

### Recommendation

Change the Rust `DaoCalculator` to read only the lowest byte of the witness `input_type` field, matching the on-chain C VM behavior:

```rust
// Current (wrong): reads full u64
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// Fixed: read only the lowest byte, matching dao.c
Ok(u64::from(header_deps_index_data.unwrap()[0]))
``` [5](#0-4) 

Additionally, add a protocol-level check that rejects transactions whose witness `input_type` index exceeds `u8::MAX`, so the on-chain and off-chain interpretations are guaranteed to agree.

---

### Proof of Concept

The repository's own test `check_dao_withdraw_header_dep_index_exceeds_u8` demonstrates the divergence:

- 258 `header_deps` are constructed; `header_deps[1]` = real deposit block; `header_deps[257]` = withdraw block.
- Witness `input_type` = `257u64` (little-endian); lowest byte = `1`.
- C VM resolves index `1` → real deposit block (block 100).
- Rust resolves index `257` → withdraw block (block 200).
- The test asserts `result.is_err()` only because the block numbers differ (100 ≠ 200). [6](#0-5) 

Replace `header_deps[257]` with any fork block at height 100 and the assertion flips to `Ok`, confirming the guard is bypassable and the Rust code silently uses the wrong block's `ar`.

### Citations

**File:** util/dao/src/lib.rs (L38-123)
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

                            let deposit_header = self
                                .data_loader
                                .get_header(deposit_header_hash)
                                .ok_or(DaoError::InvalidHeader)?;
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
                        } else {
                            Ok(output.capacity().into())
                        }
                    } else {
                        Ok(output.capacity().into())
                    }
                };
                capacity.and_then(|c| c.safe_add(capacities).map_err(Into::into))
            },
        )
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
