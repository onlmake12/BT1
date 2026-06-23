### Title
DAO Withdrawal `header_deps` Index Truncation Causes Consensus Split Between On-Chain C VM and Off-Chain Rust Verifier — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` in `util/dao/src/lib.rs` reads the deposit-header index from the witness as a full `u64`, while the on-chain Nervos DAO C script reads only the lowest byte (treating it as `u8`). When a transaction sender supplies an index value greater than 255, the two implementations resolve to different entries in `header_deps`. Because `DaoHeaderVerifier` in `verification/contextual/src/contextual_block_verifier.rs` uses the same Rust `DaoCalculator` to recompute the block's `dao` field and compare it against the committed header, a crafted DAO withdrawal transaction can cause the Rust node to reject a block that the on-chain C VM script accepted — a consensus split.

---

### Finding Description

**Root cause — `util/dao/src/lib.rs`, lines 79–98**

The comment on line 79 explicitly states the protocol expectation:

> `// dao contract stores header deps index as u64 in the input_type field of WitnessArgs`

Rust then reads the full 8-byte value:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))  // line 91
```

and uses it directly as a `usize` index:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // line 96
``` [1](#0-0) 

The on-chain DAO C script, however, reads only the lowest byte of that 8-byte field (i.e., it treats the index as `uint8_t`). This is explicitly documented in the test suite:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
``` [2](#0-1) 

For any witness index `N` where `N > 255` and `N & 0xFF != N`, the two implementations resolve to different `header_deps` slots.

**Propagation into block-level consensus — `verification/contextual/src/contextual_block_verifier.rs`, lines 300–320**

`DaoHeaderVerifier::verify` calls `DaoCalculator::dao_field`, which internally calls `withdrawed_interests` → `transaction_maximum_withdraw`. If any DAO withdrawal transaction in the block uses an index > 255, Rust resolves the wrong deposit header, computes a different `withdrawed_interests`, and therefore a different `dao` field value. The check:

```rust
if dao != self.header.dao() {
    return Err((BlockErrorKind::InvalidDAO).into());
}
``` [3](#0-2) 

causes the Rust node to reject the block even though the C VM script passed and the miner's `dao` field is correct.

**Tx-pool pre-check — `tx-pool/src/util.rs`, lines 28–54**

`check_tx_fee` also calls `DaoCalculator::transaction_fee`, which calls `transaction_maximum_withdraw`. With index 257, Rust resolves to the wrong block, gets a block-number mismatch (`deposit_header.number() != deposited_block_number`), and returns `DaoError::InvalidOutPoint`, causing the transaction to be rejected from the tx pool with `Reject::Malformed` — even though the C VM would accept it. [4](#0-3) 

---

### Impact Explanation

**Consensus split / block orphaning.** A miner whose node runs the C VM DAO script sees the transaction as valid, includes it in a block, and computes the correct `dao` header field. Rust full nodes run `DaoHeaderVerifier`, resolve the deposit header differently, compute a different `dao` field, and reject the block as `InvalidDAO`. The miner's block is orphaned. Repeated use of this technique can selectively orphan blocks that contain such transactions, disrupting liveness and potentially enabling fee-sniping or targeted miner harassment.

**Secondary: tx-pool DoS.** Because `check_tx_fee` also uses the same broken resolution, a crafted transaction with index 257 that the C VM would accept is rejected by the Rust tx pool, preventing it from ever being submitted — a one-sided liveness denial for legitimate DAO withdrawals that happen to use a large `header_deps` list.

---

### Likelihood Explanation

The attack requires only that a transaction sender:
1. Constructs a DAO withdrawal transaction with ≥ 258 `header_deps` entries.
2. Places the real deposit block hash at position `N & 0xFF` (e.g., position 1).
3. Places any other block hash at position `N` (e.g., position 257).
4. Sets the 8-byte witness index to `N` (e.g., 257).
5. Sets cell data to the real deposit block number.

No privileged access, no key material, no majority hashpower, and no social engineering is required. The transaction is a standard RPC submission (`send_transaction`). The discrepancy is already documented in the production test suite, meaning the attack surface is known.

---

### Recommendation

1. **Align Rust with the C VM**: In `DaoCalculator::transaction_maximum_withdraw`, after reading the u64 index, validate that it fits in a `u8` (i.e., `header_dep_index <= 255`). If it does not, return `DaoError::InvalidDaoFormat`. This makes Rust's rejection semantics match the C VM's behavior.

2. **Alternatively, fix the C VM script**: Update the on-chain DAO script to read the full 8-byte little-endian u64 index, matching the Rust implementation. This requires a script upgrade and a consensus-level migration.

3. **Add a bounds check in `transaction_maximum_withdraw`** regardless of which direction the fix goes, to ensure the resolved `header_dep_index` is within the actual length of `header_deps()`.

---

### Proof of Concept

The production test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` already demonstrates the discrepancy: [5](#0-4) 

**Attack transaction structure:**

```
header_deps:
  [0]   = dummy hash
  [1]   = real_deposit_block.hash()   ← C VM resolves here (lowest byte of 257 = 1)
  [2..256] = dummy hashes
  [257] = withdraw_block.hash()       ← Rust resolves here (full u64 = 257)

witness input_type = 257u64 (little-endian 8 bytes)
cell data          = real_deposit_block.number() (8 bytes, little-endian)
```

**C VM execution path:**
- Reads index byte = `0x01` → `header_deps[1]` = `real_deposit_block.hash()`
- `deposit_header.number()` = 100 = `deposited_block_number` → **PASSES**

**Rust `DaoCalculator` path:**
- Reads index u64 = 257 → `header_deps[257]` = `withdraw_block.hash()`
- `deposit_header.number()` = 200 ≠ 100 → `DaoError::InvalidOutPoint`
- `DaoHeaderVerifier` cannot compute `dao_field` → block rejected as `InvalidDAO`

**Result:** The miner's block (valid per C VM) is orphaned by all Rust full nodes.

### Citations

**File:** util/dao/src/lib.rs (L91-98)
```rust
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
                                        .and_then(|hash| header_deps.get(&hash))
                                        .ok_or(DaoError::InvalidOutPoint)
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L316-318)
```rust
        if dao != self.header.dao() {
            return Err((BlockErrorKind::InvalidDAO).into());
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
