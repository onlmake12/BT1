### Title
Wrong `header_dep_index` Width in DAO Withdrawal Validation Causes Consensus Split — (`File: util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator` reads the `header_dep_index` from the DAO withdrawal witness as a full 8-byte little-endian `u64`, while the on-chain DAO C script (`dao.c`) reads only the **lowest byte** (`uint8_t`). When a transaction sender crafts a witness whose lowest byte points to the correct deposit header but whose full `u64` value points to a different entry, the C VM accepts the transaction while the Rust node rejects it. This is a consensus split: a block that is valid under the on-chain script rules will be rejected by the Rust node.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit header index from the witness `input_type` field:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)
```

The Rust node reads all 8 bytes as a `u64` and uses that as the `header_deps` array index.

The on-chain DAO C script (referenced at `test/src/specs/dao/dao_user.rs` line 14: `dao.c#L81`) reads only the **lowest byte** of the same 8-byte field, treating it as a `uint8_t` index.

This discrepancy is explicitly documented and tested in `util/dao/src/tests.rs` at `check_dao_withdraw_header_dep_index_exceeds_u8` (lines 475–537):

- `header_deps[1]` = deposit block (block 100) — what the C VM resolves via lowest byte of `257`
- `header_deps[257]` = withdraw block (block 200) — what the Rust node resolves via full `u64` value `257`
- The test asserts `result.is_err()` — confirming the Rust node rejects a transaction the C VM would accept

The comment at line 534 states: *"Rust resolves index 257 → withdraw block (number 200), but cell data says deposited at block 100."*

---

### Impact Explanation

A transaction sender can craft a DAO withdrawal transaction where:
- The 8-byte `input_type` witness field encodes a `u64` value whose **lowest byte** is a valid deposit header index (e.g., `1`), but whose **full u64** value (e.g., `257`) points to a different entry in `header_deps`.
- The on-chain C VM accepts the transaction (lowest byte = 1 → correct deposit header).
- The Rust node rejects the transaction (full u64 = 257 → wrong header → block number mismatch → `DaoError::InvalidOutPoint`).

When a miner includes such a transaction in a block, the block passes C VM script validation but is rejected by the Rust node's `DaoCalculator`. This causes a **consensus split**: the Rust node forks away from the canonical chain, breaking liveness and potentially enabling targeted chain-split attacks against specific nodes.

---

### Likelihood Explanation

Any transaction sender who understands the DAO withdrawal format can craft this witness. No privileged access, key material, or majority hashpower is required. The attacker only needs to:
1. Have a valid DAO cell to withdraw.
2. Pad `header_deps` to at least 258 entries.
3. Set `input_type` to a `u64` whose lowest byte is the correct deposit header index but whose full value points elsewhere.

The discrepancy is already documented in the codebase's own test, confirming the root cause is known and the split is reproducible.

---

### Recommendation

In `util/dao/src/lib.rs`, change the index extraction to read only the lowest byte, matching the on-chain C script behavior:

```rust
// Before (reads full u64):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// After (reads only lowest byte, matching dao.c):
Ok(header_deps_index_data.unwrap()[0] as u64)
```

Alternatively, add a consensus-level validation that rejects any `input_type` value whose upper 7 bytes are non-zero, so both the Rust node and the C VM agree on what constitutes a valid index encoding.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–537) directly demonstrates the split: [1](#0-0) [2](#0-1) [3](#0-2) 

**Scenario:**

1. Attacker has a valid DAO cell deposited at block 100.
2. Attacker builds a withdrawal transaction with 258 `header_deps`:
   - `header_deps[1]` = deposit block hash (block 100)
   - `header_deps[257]` = any other block hash
3. Attacker sets `input_type` = `257u64` in little-endian (bytes: `[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`).
4. On-chain C VM reads lowest byte = `0x01` → index 1 → deposit block → validates successfully → **accepts**.
5. Rust `DaoCalculator` reads full u64 = `257` → index 257 → wrong block → `deposit_header.number() != deposited_block_number` → `DaoError::InvalidOutPoint` → **rejects**.
6. A miner includes the transaction; the resulting block is valid per consensus script rules but rejected by the Rust node → **chain split**. [4](#0-3)

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
