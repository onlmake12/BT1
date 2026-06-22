### Title
DAO Withdrawal `header_dep` Index Width Mismatch Between Rust Verifier and On-Chain C VM — (`File: util/dao/src/lib.rs`)

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the full 8-byte little-endian `u64` from the witness `input_type` field as the `header_deps` array index. The on-chain `dao.c` script running in CKB-VM reads only the **lowest byte** (u8) of that same 8-byte field. When `input_type > 255`, the two sides resolve to different `header_deps` entries, causing the Rust node to reject DAO withdrawal transactions that the on-chain C VM would accept.

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit-header index from the witness `input_type` field:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)   // uses full u64
        ...
``` [1](#0-0) 

The on-chain `dao.c` script interprets the same 8-byte field by reading only its **lowest byte** as the index (u8 truncation). For `input_type = 257` (little-endian bytes `[0x01, 0x01, 0x00, …]`):

- **C VM** reads byte 0 → index **1** → `header_deps[1]` = deposit block ✓
- **Rust** reads full u64 → index **257** → `header_deps[257]` = a different block ✗

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` explicitly documents this split:

```rust
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
``` [2](#0-1) 

The test asserts `result.is_err()`, confirming the Rust verifier rejects the transaction even though the C VM would accept it.

### Impact Explanation

**Primary — tx-pool censorship of valid DAO withdrawals:** The `ContextualTransactionVerifier::verify` pipeline calls `fee_calculator.transaction_fee()` (which invokes `DaoCalculator`) after script verification. If `DaoCalculator` returns `DaoError::InvalidOutPoint` for a transaction the C VM accepted, the transaction is permanently rejected from the tx-pool. A DAO depositor whose wallet encodes `input_type > 255` cannot withdraw funds through any standard node. [3](#0-2) 

**Secondary — wrong DAO field in mined blocks:** `DaoCalculator` is also called from `dao_field_with_current_epoch` → `withdrawed_interests` → `transaction_maximum_withdraw` during block assembly. If a DAO withdrawal with `input_type > 255` is included in a block (e.g., by a miner bypassing the tx-pool), the Rust node computes the wrong withdrawal amount, writes an incorrect DAO accumulator field into the block header, and other nodes reject the block — causing a consensus failure for that miner. [4](#0-3) 

### Likelihood Explanation

A transaction sender (DAO depositor) controls the `input_type` witness field entirely. Encoding `input_type = 256` (bytes `[0x00, 0x01, 0x00, …]`) with the correct deposit header at `header_deps[0]` (C VM index = 0) and any block at `header_deps[256]` (Rust index = 256) is sufficient to trigger the mismatch. The test demonstrates 258 `header_deps` entries are constructible. Any wallet or script that uses an index ≥ 256 — whether by design or by having many header deps — hits this path. The discrepancy is already documented in the test suite, confirming the developers are aware it is reachable.

### Recommendation

Replace the full-u64 read with a single-byte read to match the on-chain `dao.c` behavior:

```rust
// Before (reads all 8 bytes):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// After (reads only the lowest byte, matching dao.c):
Ok(header_deps_index_data.unwrap()[0] as u64)
``` [5](#0-4) 

Alternatively, if the protocol intends to support indices > 255, the `dao.c` script must be updated to read the full 8-byte u64, and a hardfork must be coordinated. Either way, the two sides must agree on the same index width.

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` is a direct proof of concept:

1. Build a DAO withdrawal transaction with 258 `header_deps`, `header_deps[1]` = deposit block (block 100), `header_deps[257]` = withdraw block (block 200), and `input_type = 257`.
2. C VM resolves lowest byte → index 1 → deposit block (block 100) → block number matches cell data → **C VM accepts**.
3. Rust `DaoCalculator` resolves full u64 → index 257 → withdraw block (block 200) → block number 200 ≠ cell data 100 → `DaoError::InvalidOutPoint` → **Rust rejects**.
4. The test asserts `result.is_err()`, confirming the Rust node incorrectly rejects a transaction the on-chain script would accept. [6](#0-5)

### Citations

**File:** util/dao/src/lib.rs (L38-124)
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

**File:** verification/src/transaction_verifier.rs (L162-171)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
```
