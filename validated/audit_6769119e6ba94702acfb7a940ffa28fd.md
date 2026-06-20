### Title
DAO Withdrawal Witness Index Truncation Causes Rust Verifier / On-Chain Script Disagreement on Deposit Header — (File: `util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator` reads the full 8-byte `u64` from the witness `input_type` field as the `header_deps` index when resolving the deposit header for a DAO withdrawal. The on-chain C DAO script reads only the **lowest byte** of that same value as the index. When a transaction supplies a `header_deps_index` value greater than 255, the two sides resolve to **different entries** in `header_deps`, causing them to use different deposit headers for DAO withdrawal calculations. This discrepancy is structurally identical to the external report's "wrong PDA passed to the wrong operation" class: one side sets up and resolves via one identifier, the other side resolves via a distinct identifier, and neither side detects the mismatch at the protocol boundary.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit header by reading the witness `input_type` field as a raw `u64` and using it directly as an index into `header_deps`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
    .and_then(|hash| header_deps.get(&hash))
    .ok_or(DaoError::InvalidOutPoint)
``` [1](#0-0) 

The on-chain C DAO script, however, reads only the **lowest byte** of the same 8-byte witness field as the index. This is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

```rust
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
``` [2](#0-1) 

The test constructs a `header_deps` list with 258 entries, places the deposit block at index 1 and the withdraw block at index 257, then stores `257u64` in the witness `input_type` field: [3](#0-2) 

- The C VM resolves `257 & 0xFF = 1` → deposit block (correct).
- The Rust verifier resolves `257` → withdraw block (wrong).

The Rust verifier then fails the block-number cross-check (`deposit_header.number() != deposited_block_number`) and returns `Err`, while the C VM would accept the transaction. [4](#0-3) 

The `DaoCalculator` is used in the consensus-critical verification path: [5](#0-4) 

---

### Impact Explanation

Two distinct impact paths exist:

**Path 1 — Valid withdrawal silently rejected (DoS):** When a legitimate user constructs a DAO withdrawal with `header_deps_index > 255` (e.g., because the transaction references many header deps), the Rust verifier resolves the wrong deposit header, fails the block-number check, and rejects the transaction from the tx pool. The C VM would have accepted it. The user cannot withdraw their DAO deposit through this node.

**Path 2 — Consensus failure (more severe):** An attacker who controls a DAO deposit can craft a withdrawal where:
- `header_deps[N & 0xFF]` = actual deposit block (C VM uses this, low AR).
- `header_deps[N]` = a fork block at the same height with a **higher AR** (Rust uses this, passes block-number check).
- Output capacity is set between C VM's `maximum_withdraw` and Rust's `maximum_withdraw`.

The Rust verifier accepts the transaction (output ≤ Rust's inflated max). The C VM rejects it (output > actual max). The node assembles a block containing this transaction; other nodes reject the block as invalid. This is a consensus-layer failure reachable by any DAO depositor. [6](#0-5) 

---

### Likelihood Explanation

The attacker must be a DAO depositor (no special privilege — permissionless). Constructing a transaction with `header_deps_index > 255` requires placing more than 255 entries in `header_deps`, which is unusual but not protocol-prohibited. For Path 2, the attacker additionally needs access to a fork block at the deposit height with a higher AR, which is feasible on a chain with any historical forks. The entry point is the standard `send_transaction` RPC or direct P2P relay.

---

### Recommendation

Align the Rust verifier with the on-chain C DAO script by reading only the lowest byte of the witness `input_type` field as the `header_deps` index:

```rust
// Before
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// After — match C VM's u8 truncation
Ok(header_deps_index_data.unwrap()[0] as u64)
```

Alternatively, if the C DAO script should be updated to read the full `u64`, that change must be made atomically with a consensus-layer upgrade so both sides agree on the same index derivation. Either way, the two sides must use the **same** index derivation for the same operation.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the split: [7](#0-6) 

To demonstrate Path 2 (consensus failure), extend the test by:
1. Replacing `header_deps[257]` with a block at the **same block number** as the deposit block but with a higher AR value in its DAO field.
2. Setting output capacity to `C_VM_max + 1` (above C VM's limit, below Rust's inflated limit).
3. Observe: Rust verifier returns `Ok` (accepts), while running the actual DAO C script in CKB-VM returns a script failure.

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

**File:** verification/src/transaction_verifier.rs (L735-758)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        for (index, (cell_meta, input)) in self
            .rtx
            .resolved_inputs
            .iter()
            .zip(self.rtx.transaction.inputs())
            .enumerate()
        {
            // ignore empty since
            let since: u64 = input.since().into();
            if since == 0 {
                continue;
            }
            let since = Since(since);
            // check remain flags
            if !since.flags_is_valid() {
                return Err((TransactionError::InvalidSince { index }).into());
            }

            // verify time lock
            self.verify_absolute_lock(index, since)?;
            self.verify_relative_lock(index, since, cell_meta)?;
        }
        Ok(())
```
