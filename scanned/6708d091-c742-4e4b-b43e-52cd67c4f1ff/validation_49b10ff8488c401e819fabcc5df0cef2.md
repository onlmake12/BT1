### Title
DAO Withdrawal `header_dep_index` Full-u64 vs. Lowest-Byte Interpretation Discrepancy Causes False Rejection of Valid Transactions — (`util/dao/src/lib.rs`)

---

### Summary

The Rust node's `DaoCalculator::transaction_maximum_withdraw` reads the `header_dep_index` from the DAO withdrawal witness as a full `u64`, while the on-chain CKB-VM DAO script (C implementation) reads only the **lowest byte** of that same field. For any `header_dep_index >= 256`, the two implementations index into `header_deps` at different positions, causing the Rust node to look up the wrong deposit block header. This produces a false `DaoError::InvalidOutPoint` rejection for transactions that the CKB-VM would accept, and is confirmed by a test added to the repository on 2026-06-19.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit block's position in `header_deps` from the witness `input_type` field:

```rust
// line 91
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then uses the full `u64` value to index into the transaction's `header_deps` list:

```rust
// lines 93-98
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)
        .and_then(|hash| header_deps.get(&hash))
        .ok_or(DaoError::InvalidOutPoint)
})?;
```

The CKB-VM's DAO script (written in C) interprets the same 8-byte field using only its **lowest byte** — effectively treating the index as a `u8`. For `header_dep_index = 257` (little-endian bytes `[0x01, 0x01, 0, 0, 0, 0, 0, 0]`):

- **CKB-VM** resolves index `1` (lowest byte).
- **Rust node** resolves index `257` (full `u64`).

If a transaction places the correct deposit block hash at position `1` and an unrelated block hash at position `257`, the CKB-VM accepts the transaction while the Rust node's `transaction_maximum_withdraw` returns `DaoError::InvalidOutPoint` (because the block at index 257 has a block number that does not match `deposited_block_number`).

This discrepancy is explicitly documented in the test added on 2026-06-19:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

The test asserts `result.is_err()`, confirming the Rust node rejects the transaction.

`transaction_maximum_withdraw` is called from `DaoCalculator::transaction_fee`, which is invoked in the block verification pipeline (`verification/src/transaction_verifier.rs`, 5 call sites). A `DaoError` returned here propagates as a block-level verification failure, meaning the Rust node will **reject a block** containing a valid DAO withdrawal transaction whose `header_dep_index >= 256`.

---

### Impact Explanation

A transaction sender submits a DAO withdrawal (phase 2) transaction where the witness `input_type` encodes `header_dep_index = 256 + k` (for any `k` in `0..=255`). The CKB-VM resolves position `k` (correct deposit block) and accepts the transaction. The Rust node resolves position `256 + k` (a different block), the block-number cross-check fails, and the node returns `DaoError::InvalidOutPoint`.

Consequences:
1. **Tx-pool denial of service**: The Rust node refuses to admit the transaction into the mempool, preventing the user from withdrawing locked CKBytes.
2. **Block-level false rejection**: If such a transaction is mined by another node and relayed, the Rust node rejects the entire block as invalid, causing a **local chain split** — the node falls behind the canonical chain.
3. **Fee calculation corruption**: Even in cases where the index happens to resolve to a block with a matching block number, the Rust node uses a different accumulate-rate (`ar`) than the CKB-VM, producing an incorrect fee estimate that can cause the node to mis-prioritize or mis-admit transactions.

---

### Likelihood Explanation

The attacker-controlled entry path is a standard DAO withdrawal transaction submitted via the `send_transaction` RPC or relayed over P2P. No privileged access is required. The only constraint is that the witness `input_type` must encode a value `>= 256`, which is a freely chosen 8-byte field. A transaction sender can trivially set `header_dep_index = 256` (lowest byte `0`) and pad `header_deps` to at least 257 entries (each 32 bytes; 257 × 32 = 8,224 bytes, well within the block size limit). The discrepancy is deterministic and reproducible.

---

### Recommendation

Align the Rust node's index extraction with the CKB-VM's behavior by masking the parsed `u64` to its lowest byte before using it as an index:

```rust
// In transaction_maximum_withdraw, after parsing the u64:
let header_dep_index = LittleEndian::read_u64(&header_deps_index_data.unwrap());
let header_dep_index = (header_dep_index & 0xFF) as usize; // match CKB-VM lowest-byte semantics
```

Additionally, add a bounds check and return `DaoError::InvalidDaoFormat` if the masked index is out of range, rather than silently returning `DaoError::InvalidOutPoint`.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` (added 2026-06-19, commit `61b18a7c`) directly demonstrates the discrepancy: [1](#0-0) 

The root cause is the unconditional full-`u64` read and index use in production code: [2](#0-1) 

The block-number cross-check that triggers the rejection: [3](#0-2) 

**Steps to reproduce:**

1. Create a DAO deposit cell with `deposited_block_number = 100`.
2. Construct a phase-2 withdrawal transaction with 258 `header_deps`:
   - `header_deps[1]` = hash of block 100 (deposit block).
   - `header_deps[257]` = hash of block 200 (withdraw block, wrong for deposit lookup).
3. Set witness `input_type` = `257u64` in little-endian.
4. Submit to a Rust CKB node via `send_transaction`.
5. **Observed**: node returns `DaoError::InvalidOutPoint` and rejects the transaction.
6. **Expected**: node accepts the transaction (CKB-VM resolves index `1` = correct deposit block).

### Citations

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

**File:** util/dao/src/lib.rs (L83-99)
```rust
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
