### Title
DAO Withdrawal Deposit-Header Lookup Uses Full u64 Witness Index While On-Chain Script Uses Only Lowest Byte — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` in `util/dao/src/lib.rs` reads the deposit header-dep index from the witness as a full `u64` and uses it directly to index into `header_deps`. The on-chain C DAO script reads only the **lowest byte** (u8) of the same 8-byte witness field. When a DAO withdrawal transaction carries a witness index whose value exceeds 255 but whose lowest byte points to the correct deposit header, the Rust node resolves a different header dep than the C VM script does. The Rust node then rejects the transaction (or the block containing it) while the C VM script accepts it, producing a consensus split.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit header-dep index from the witness `input_type` field as a full 8-byte little-endian `u64`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then uses that value directly as a `usize` array index:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
    .and_then(|hash| header_deps.get(&hash))
    .ok_or(DaoError::InvalidOutPoint)
```

The on-chain C DAO script, however, interprets only the **lowest byte** of the same 8-byte field as the index. This is the exact discrepancy documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs`:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

A transaction sender crafts a DAO withdrawal with 258 header deps, places the deposit block hash at position 1 and the withdraw block hash at position 257, and sets the witness `input_type` to `257u64` (little-endian bytes: `[0x01, 0x01, 0x00, …]`). The C VM reads the lowest byte `0x01` → resolves `header_deps[1]` = deposit block → block-number check passes → script succeeds. The Rust `DaoCalculator` reads the full value `257` → resolves `header_deps[257]` = withdraw block → `deposit_header.number()` (200) ≠ `deposited_block_number` (100) → returns `DaoError::InvalidOutPoint`.

`DaoCalculator::transaction_fee` is called by `FeeCalculator` inside `ContextualTransactionVerifier::verify`, and `DaoCalculator::dao_field` (which internally calls `withdrawed_interests` → `transaction_maximum_withdraw`) is called by `DaoHeaderVerifier` during contextual block verification. Both paths reject the transaction or the block when the Rust index resolution diverges from the C VM's.

---

### Impact Explanation

**Consensus split / block rejection.** A miner who runs the C VM script correctly will produce a block containing a valid DAO withdrawal (script passes). The Rust node's `DaoHeaderVerifier` recomputes the DAO field using `DaoCalculator`, resolves the wrong deposit header, and arrives at a different DAO accumulation ratio, causing it to reject the block as `InvalidDAO`. Nodes that have not yet applied the fix will reject blocks that are valid under the C VM consensus rules, splitting the network. Additionally, any legitimate DAO depositor who constructs a withdrawal with more than 255 header deps and a witness index > 255 will have their transaction permanently rejected from the tx-pool and from block verification on Rust nodes, constituting a targeted denial of service against DAO withdrawals.

**Impact: 4** — consensus split and permanent DAO withdrawal denial for affected transactions.

---

### Likelihood Explanation

Constructing a DAO withdrawal with 258 header deps is unusual but requires no privilege: any transaction sender can include arbitrary header deps. The witness field is fully attacker-controlled. The discrepancy is already documented in a test in the repository, confirming the developers are aware of the behavioral difference. A motivated attacker or a user who accidentally triggers the edge case (e.g., a wallet that pads header deps) can reach this path.

**Likelihood: 3** — requires crafting a non-standard but protocol-legal transaction; no keys or special access needed.

---

### Recommendation

In `DaoCalculator::transaction_maximum_withdraw`, truncate the witness index to a `u8` before using it as the `header_deps` array index, to match the on-chain C DAO script's behavior:

```rust
// Read only the lowest byte, matching the C DAO script's index interpretation
let header_dep_index = LittleEndian::read_u64(&header_deps_index_data.unwrap()) as u8 as usize;
```

Alternatively, add a consensus-level check that rejects any DAO withdrawal whose witness index value exceeds 255, so both the Rust node and the C VM agree on the set of valid transactions.

---

### Proof of Concept

The repository's own test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` is the proof of concept: [1](#0-0) 

It constructs a transaction with 258 header deps, places the deposit block at index 1 and the withdraw block at index 257, sets the witness `input_type` to `257u64`, and asserts `result.is_err()` — confirming the Rust node rejects what the C VM accepts.

The root cause is in `transaction_maximum_withdraw`: [2](#0-1) 

The full-u64 read at line 91 diverges from the C VM's lowest-byte read, causing the Rust node to resolve `header_deps[257]` (the withdraw block) instead of `header_deps[1]` (the deposit block). The subsequent block-number check at line 105 then fails: [3](#0-2) 

This rejection propagates through `FeeCalculator` in `ContextualTransactionVerifier::verify` and through `DaoHeaderVerifier` in `ContextualBlockVerifier::verify`, causing the Rust node to reject both the transaction and any block containing it, while the C VM script accepts both. [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** util/dao/src/tests.rs (L476-536)
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
```

**File:** util/dao/src/lib.rs (L88-99)
```rust
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
