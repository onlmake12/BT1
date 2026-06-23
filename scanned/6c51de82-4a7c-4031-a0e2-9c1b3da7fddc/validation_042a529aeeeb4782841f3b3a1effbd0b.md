### Title
DAO Withdrawal Witness Index Parsed as u64 in Rust vs u8 in On-Chain C Script Causes Consensus Split — (`File: util/dao/src/lib.rs`)

### Summary
The Rust `DaoCalculator` reads the DAO withdrawal witness `input_type` field as a full **u64** index into `header_deps`, while the on-chain `dao.c` C script reads only the **lowest byte (u8)**. A transaction crafted with a witness index > 255 (e.g., 257 = `0x0101`) causes the C VM and the Rust verifier to resolve different deposit block headers. The C VM accepts the transaction; the Rust `DaoHeaderVerifier` rejects the block with `InvalidDAO`. This is a consensus split reachable by any miner.

### Finding Description

The `DaoCalculator::transaction_maximum_withdraw()` function in `util/dao/src/lib.rs` resolves the deposit block header for a DAO withdrawal by reading the witness `input_type` field as a full 8-byte little-endian u64 and using it as an index into `header_deps`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // full u64 used as index
``` [1](#0-0) 

The on-chain `dao.c` C script (referenced at `test/src/specs/dao/dao_user.rs:14`) reads the same field as a **u8** (lowest byte only). This discrepancy is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
``` [2](#0-1) 

An attacker crafts a DAO phase-2 withdrawal transaction with 258 `header_deps` entries, witness index = 257 (`[0x01, 0x01, 0x00, ...]`):

- **C VM** reads lowest byte = 1 → `header_deps[1]` = deposit block → block number matches cell data → **script passes**
- **Rust** reads full u64 = 257 → `header_deps[257]` = withdraw block → block number ≠ cell data → `DaoError::InvalidOutPoint` [3](#0-2) 

The `DaoHeaderVerifier` in block verification calls `DaoCalculator::dao_field()` → `withdrawed_interests()` → `transaction_maximum_withdraw()`. When this returns an error, the verifier returns `BlockErrorKind::InvalidDAO` and rejects the block:

```rust
pub fn verify(&self) -> Result<(), Error> {
    let dao = DaoCalculator::new(...)
        .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
        .map_err(|e| { ... e })?;
    if dao != self.header.dao() {
        return Err((BlockErrorKind::InvalidDAO).into());
    }
    Ok(())
}
``` [4](#0-3) 

`DaoHeaderVerifier::verify()` is called unconditionally (unless `disable_daoheader` switch is set) during `ContextualBlockVerifier::verify()`: [5](#0-4) 

### Impact Explanation

A block containing the crafted transaction passes CKB-VM script execution (the authoritative on-chain rule via `dao.c`) but is rejected by every Rust node with `InvalidDAO`. Any miner who includes this transaction in a mined block causes a **consensus split**: the block is valid per the C VM but invalid per the Rust implementation. Nodes running the Rust implementation will reject the block and its descendants, diverging from any implementation that follows the C VM's u8 interpretation. The `S_i` (secondary issuance surplus) field in the DAO header is also computed incorrectly for any block containing such a transaction, compounding the divergence.

### Likelihood Explanation

Any miner with any amount of hashpower can craft and include this transaction directly in a block (bypassing the tx-pool, which also rejects it via `check_tx_fee` for the same reason). The crafted transaction is structurally valid: it has a well-formed witness, valid `header_deps` entries, and passes C VM script execution. No privileged access, leaked keys, or majority hashpower is required — a single mined block suffices to trigger the split.

### Recommendation

Align the Rust `DaoCalculator` with the on-chain `dao.c` behavior by reading only the lowest byte of the witness `input_type` index, or update `dao.c` to read the full u64. The fix must be applied consistently to `transaction_maximum_withdraw()` in `util/dao/src/lib.rs` and to any other location that interprets this index. A consensus-layer hard fork may be required if `dao.c` is changed.

### Proof of Concept

```
1. Construct a DAO phase-2 withdrawal transaction:
   - cell_data = deposit_block_number (e.g., 100) as u64 LE
   - header_deps = [dummy; 258]
     header_deps[1]   = deposit_block.hash()   // C VM resolves here (byte 0x01)
     header_deps[257] = withdraw_block.hash()  // Rust resolves here (u64 = 257)
   - witness input_type = 257u64.to_le_bytes() = [0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]

2. Include this transaction in a mined block.

3. C VM (dao.c): reads index byte = 0x01 → header_deps[1] = deposit block
   → deposit_header.number() == 100 == cell_data → script PASSES

4. Rust DaoHeaderVerifier: reads index u64 = 257 → header_deps[257] = withdraw block
   → deposit_header.number() == withdraw_block_number ≠ 100
   → DaoError::InvalidOutPoint → BlockErrorKind::InvalidDAO → block REJECTED

5. Result: consensus split. The block is valid per the on-chain C VM rule
   but rejected by all Rust nodes.
```

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–537) already demonstrates step 4 in isolation, confirming the Rust rejection path. [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** util/dao/src/lib.rs (L312-333)
```rust
    fn withdrawed_interests(
        &self,
        mut rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
    ) -> Result<Capacity, DaoError> {
        let maximum_withdraws = rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
            self.transaction_maximum_withdraw(rtx)
                .and_then(|c| capacities.safe_add(c).map_err(Into::into))
        })?;
        let input_capacities = rtxs.try_fold(Capacity::zero(), |capacities, rtx| {
            let tx_input_capacities = rtx.resolved_inputs.iter().try_fold(
                Capacity::zero(),
                |tx_capacities, cell_meta| {
                    let output_capacity: Capacity = cell_meta.cell_output.capacity().into();
                    tx_capacities.safe_add(output_capacity)
                },
            )?;
            capacities.safe_add(tx_input_capacities)
        })?;
        maximum_withdraws
            .safe_sub(input_capacities)
            .map_err(Into::into)
    }
```

**File:** util/dao/src/tests.rs (L489-495)
```rust
    // Pad header_deps to 258 entries so index 257 is valid.
    // Position 1: correct deposit block (what C VM resolves via lowest byte).
    // Position 257: withdraw block (wrong — Rust resolves this with full u64).
    let dummy = h256!("0x1").into();
    let mut header_deps = vec![dummy; 258];
    header_deps[1] = deposit_block.hash();
    header_deps[257] = withdraw_block.hash();
```

**File:** util/dao/src/tests.rs (L512-536)
```rust
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
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L278-321)
```rust
struct DaoHeaderVerifier<'a, 'b, 'c, CS> {
    context: &'a VerifyContext<CS>,
    resolved: &'a [Arc<ResolvedTransaction>],
    parent: &'b HeaderView,
    header: &'c HeaderView,
}

impl<'a, 'b, 'c, CS: ChainStore + VersionbitsIndexer> DaoHeaderVerifier<'a, 'b, 'c, CS> {
    pub fn new(
        context: &'a VerifyContext<CS>,
        resolved: &'a [Arc<ResolvedTransaction>],
        parent: &'b HeaderView,
        header: &'c HeaderView,
    ) -> Self {
        DaoHeaderVerifier {
            context,
            resolved,
            parent,
            header,
        }
    }

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
}
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L670-672)
```rust
        if !self.switch.disable_daoheader() {
            DaoHeaderVerifier::new(&self.context, resolved, &parent, &block.header()).verify()?;
        }
```
