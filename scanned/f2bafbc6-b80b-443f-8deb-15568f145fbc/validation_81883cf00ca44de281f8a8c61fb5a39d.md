Audit Report

## Title
DAO Withdrawal Deposit-Header Index Resolved Differently by Rust Node vs. On-Chain C Script — Rust Node Can Accept Transactions the C VM Rejects - (File: `util/dao/src/lib.rs`)

## Summary

`transaction_maximum_withdraw` in `util/dao/src/lib.rs` reads the deposit-header index from the witness as a full `u64` and indexes into `header_deps()` with that value directly. The on-chain DAO C script (running inside CKB-VM) reads the same 8-byte field but uses only its **lowest byte** as the index. When a witness index ≥ 256 is supplied, the two implementations resolve different entries in `header_deps`, causing the Rust node to compute a different deposit header than the C VM enforces. A crafted transaction can pass Rust's fee validation while being rejected by the C VM, causing a Rust miner to produce a block the rest of the network rejects.

## Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the index as a full `u64`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

and immediately uses it as a `usize` array index:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
```

The on-chain DAO C script uses only the **lowest byte** of the same 8-byte field as the index into `header_deps`. This divergence is explicitly documented in the test added to the codebase (`check_dao_withdraw_header_dep_index_exceeds_u8`):

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

**Exploitable scenario (Rust accepts, C VM rejects):**

Construct a transaction with ≥ 257 `header_deps` where:
- `header_deps[0]` = arbitrary block with number P ≠ M
- `header_deps[256]` = actual deposit block with number M
- Cell data = M (deposited block number)
- Witness `input_type` = `256u64` (little-endian 8 bytes; lowest byte = 0)

| Component | Rust (full u64 = 256) | C VM (lowest byte = 0) |
|---|---|---|
| Resolved entry | `header_deps[256]` = deposit block (M) | `header_deps[0]` = wrong block (P) |
| Block-number check | M == M → **PASS** | P ≠ M → **FAIL** |
| Outcome | Accepts, computes interest | Rejects transaction |

The existing test only covers the inverse direction (index 257: Rust rejects because it resolves the withdraw block; C VM would accept because lowest byte 1 resolves the deposit block). The symmetric case — where Rust accepts and C VM rejects — is not tested and is the actual attack vector.

The block-number cross-check at lines 105–107 does not protect against this scenario because Rust resolves the correct deposit block (number M matches cell data M), so the check passes on the Rust side.

## Impact Explanation

A Rust miner that includes such a transaction produces a block that every other node running the C VM rejects. This is a **consensus split / invalid-block production** vulnerability. It maps directly to the allowed CKB bounty impact: **"Vulnerabilities which could easily cause consensus deviation" (Critical, 15001–25000 points)**. The miner's block is orphaned by the rest of the network, and the network forks.

## Likelihood Explanation

The attack requires no special privilege. Any user can submit a DAO phase-2 withdrawal transaction with ≥ 257 `header_deps` (257 × 32 bytes ≈ 8 KB, well within CKB's transaction size limit) and a witness index whose full `u64` value points to the real deposit block while its lowest byte points to a different block. The Rust tx-pool's fee validation calls `transaction_fee` → `transaction_maximum_withdraw`, which accepts the transaction. The full consensus-split impact materialises when the Rust node is also a miner — a common mainnet configuration. Likelihood is **low-to-medium**: crafting the transaction is trivial; impact requires the Rust node to mine.

## Recommendation

In `transaction_maximum_withdraw`, truncate `header_dep_index` to its lowest byte before indexing into `header_deps()`, matching the on-chain C script's behaviour:

```rust
let index = (header_dep_index & 0xFF) as usize;
rtx.transaction.header_deps().get(index)
```

Additionally, add a consensus-level integration test that submits a DAO withdrawal with a witness index ≥ 256 to a live node and verifies that the block is accepted by the C VM, ensuring Rust and C VM agree on the resolved deposit header for all valid index values.

## Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–537) documents the discrepancy. Extend it to the symmetric (exploitable) case:

```rust
// header_deps[0]   = other block (number 999) — C VM resolves here (lowest byte of 256 = 0)
// header_deps[256] = deposit block (number 100) — Rust resolves here (full u64 = 256)
// Cell data = 100, witness index = 256

let mut header_deps = vec![other_block.hash()]; // position 0: number 999
header_deps.extend(vec![h256!("0x1").into(); 255]); // positions 1-255: dummies
header_deps.push(deposit_block.hash()); // position 256: number 100

let witness = WitnessArgs::new_builder()
    .input_type(Some(Bytes::from(256u64.to_le_bytes().to_vec())))
    .build();
// Cell data = 100 (deposit block number)
// Rust: header_deps[256].number() == 100 == cell_data → PASS → accepts
// C VM: header_deps[0].number()   == 999 != cell_data → FAIL → rejects
// Result: Rust miner produces a block the C VM network rejects → consensus split
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** util/dao/src/lib.rs (L91-91)
```rust
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

**File:** util/dao/src/lib.rs (L93-99)
```rust
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
