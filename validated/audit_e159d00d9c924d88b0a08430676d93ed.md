### Title
DAO Withdrawal `header_dep_index` Representation Mismatch Between Rust Node and C VM DAO Script Enables Consensus Split - (File: `util/dao/src/lib.rs`)

### Summary

The Rust node's `DaoCalculator::transaction_maximum_withdraw` reads the `header_dep_index` from the DAO withdrawal witness as a full 64-bit little-endian integer, while the on-chain C VM DAO script reads only the **lowest byte** of the same 8-byte field. A transaction sender can craft a DAO withdrawal with `header_dep_index >= 256` that the C VM DAO script accepts (resolving to the correct deposit block via the lowest byte) but that the Rust node rejects (resolving to a wrong or nonexistent block via the full u64), causing a consensus split.

### Finding Description

In `util/dao/src/lib.rs`, `DaoCalculator::transaction_maximum_withdraw` extracts the deposit header by reading the witness `input_type` field as a full `u64` index into `header_deps`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)
        ...
```

The on-chain DAO C script, however, reads this same 8-byte field using only its **lowest byte** (effectively treating it as a `uint8_t`). For any `header_dep_index` value where the full u64 and the lowest byte differ — i.e., any value `>= 256` — the two systems resolve to different entries in `header_deps`.

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` explicitly documents this discrepancy:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
// input_type = 257, lowest byte = 1
```

With `header_dep_index = 257` (LE bytes: `[0x01, 0x01, 0x00, ...]`):
- **C VM**: reads lowest byte `0x01` → resolves `header_deps[1]` = deposit block → **accepts**
- **Rust node**: reads full u64 `257` → resolves `header_deps[257]` = wrong/nonexistent block → **rejects** with `DaoError::InvalidOutPoint` or `DaoError::InvalidHeader`

The Rust node's block number cross-check at line 105 (`if deposit_header.number() != deposited_block_number`) catches the mismatch when the wrong block is resolved, causing the Rust node to reject the transaction. If a miner includes such a transaction in a block, Rust-based nodes reject the block while the canonical script execution (C VM) accepts it — a consensus split.

### Impact Explanation

A transaction sender can craft a DAO withdrawal transaction with `header_dep_index >= 256` that passes C VM script execution but is rejected by the Rust node's `DaoCalculator` during block verification. If a miner includes this transaction in a block, Rust-based full nodes reject the block, causing a **consensus split**. Nodes that evaluate script execution directly (C VM path) would accept the block; Rust nodes would not. This can be used to partition the network or stall block propagation.

### Likelihood Explanation

The attack requires only a transaction sender with a valid DAO deposit cell. The attacker constructs a `header_deps` list with 258+ entries, places the deposit block hash at index 1, and sets `witness.input_type` to `257` (u64 LE). This is fully within the capabilities of an unprivileged RPC caller or tx-pool submitter. No privileged access, key material, or majority hashpower is required.

### Recommendation

Align the Rust node's index interpretation with the C VM DAO script. Either:
1. In `DaoCalculator::transaction_maximum_withdraw`, truncate `header_dep_index` to its lowest byte before indexing: `header_dep_index as u8 as usize`, or
2. Add an explicit validation that rejects any `header_dep_index >= 256` with `DaoError::InvalidDaoFormat` before the lookup, matching the C VM's effective behavior.

### Proof of Concept

Attacker constructs a DAO withdrawal transaction:
- `header_deps`: 258 entries; `header_deps[1]` = deposit block hash; `header_deps[257]` = any other block hash
- `witness[i].input_type`: `257u64` encoded as 8-byte little-endian (`[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`)
- Cell data: deposit block number (e.g., `100u64` LE)

**C VM execution**: reads lowest byte of `257` = `1` → `header_deps[1]` = deposit block (number 100) → block number matches cell data → script **accepts**.

**Rust node `DaoCalculator`**: reads full u64 `257` → `header_deps[257]` = wrong block (e.g., number 200) → `deposit_header.number() (200) != deposited_block_number (100)` → returns `DaoError::InvalidOutPoint` → block **rejected**.

Miner submits block containing this transaction → Rust full nodes reject the block → consensus split. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** util/dao/src/tests.rs (L489-536)
```rust
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
