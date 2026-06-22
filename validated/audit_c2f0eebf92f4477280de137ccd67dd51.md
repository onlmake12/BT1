### Title
DAO Withdrawal Deposit-Header Index Resolved Differently by Rust Node vs. On-Chain C Script — Rust Node Can Accept Transactions the C VM Rejects - (File: `util/dao/src/lib.rs`)

---

### Summary

In `util/dao/src/lib.rs`, the function `transaction_maximum_withdraw` reads the deposit-header index from the witness as a full `u64` and casts it to `usize`. The on-chain DAO C script (running inside CKB-VM) reads only the **lowest byte** of that same index. When a transaction sender supplies a witness index ≥ 256, the Rust node and the C VM resolve different entries in `header_deps`, causing the Rust node to compute a different deposit header — and therefore a different interest amount — than the C VM enforces. The Rust node can accept a DAO withdrawal that the C VM will reject, causing a Rust miner to produce an invalid block that the rest of the network rejects.

---

### Finding Description

`transaction_maximum_withdraw` extracts the deposit-header index from the witness `input_type` field as a little-endian `u64`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then indexes into `header_deps()` with a direct `as usize` cast:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // ← full u64 used as index
```

The on-chain DAO C script reads the same 8-byte field but uses only its **lowest byte** as the index into `header_deps`. This is explicitly documented in the test added to the codebase:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

The existing test (`check_dao_withdraw_header_dep_index_exceeds_u8`) only verifies the case where the Rust node **rejects** a transaction the C VM would accept (because the block-number cross-check catches the mismatch). It does **not** cover the symmetric case where the Rust node **accepts** a transaction the C VM rejects.

**Exploitable scenario (Rust accepts, C VM rejects):**

| Position | Block | Block number |
|---|---|---|
| `header_deps[0]` (lowest byte of 256) | arbitrary block | P ≠ M |
| `header_deps[256]` (full u64 = 256) | actual deposit block | M |

- Cell data stores `deposited_block_number = M`.
- Witness `input_type` = `256u64` (little-endian).
- **Rust node:** resolves index 256 → deposit block (number M) → block-number check passes → computes interest using deposit block M's `ar`.
- **C VM:** resolves lowest byte 0 → `header_deps[0]` (number P ≠ M) → block-number check fails → **rejects** the transaction.

The Rust node's `transaction_fee` returns a valid (non-negative) result, so the transaction is admitted to the tx-pool. If the Rust node is also a miner, it includes the transaction in a block. Every other node running the C VM rejects that block, causing a consensus split. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

A transaction sender can craft a DAO phase-2 withdrawal with ≥ 257 `header_deps` and a witness index whose full `u64` value points to the real deposit block while its lowest byte points to a different block. The Rust node's fee validator accepts the transaction; the C VM rejects it. A Rust miner that includes the transaction produces a block the rest of the network rejects. This is a **consensus split / invalid-block production** vulnerability triggered by an unprivileged transaction sender. [4](#0-3) [5](#0-4) 

---

### Likelihood Explanation

The attack requires a transaction with ≥ 257 `header_deps` (257 × 32 bytes = ~8 KB, well within CKB's transaction size limit). No special privilege is needed to submit such a transaction to the tx-pool. The Rust node's tx-pool validation calls `transaction_fee` → `transaction_maximum_withdraw`, which accepts the transaction. The only additional requirement for the full consensus-split impact is that the Rust node is also a miner — a common configuration on mainnet. Likelihood is **low-to-medium**: crafting the transaction is trivial; the impact materialises only when the Rust node mines. [6](#0-5) 

---

### Recommendation

**Short term:** In `transaction_maximum_withdraw`, truncate `header_dep_index` to its lowest byte before indexing into `header_deps()`, matching the on-chain C script's behaviour:

```rust
let index = (header_dep_index & 0xFF) as usize;
rtx.transaction.header_deps().get(index)
```

**Long term:** Add a consensus-level test that submits a DAO withdrawal with a witness index ≥ 256 to a live node and verifies that the block containing it is accepted (not rejected) by the C VM, ensuring Rust and C VM agree on the resolved deposit header for all valid index values. [7](#0-6) 

---

### Proof of Concept

The existing test already documents the discrepancy. Extend it to the symmetric case:

```rust
// header_deps[256] = deposit block (number 100)
// header_deps[0]   = some other block (number 999)
// witness index    = 256  (lowest byte = 0)
//
// Rust resolves index 256 → deposit block (number 100) → check passes
// C VM  resolves index   0 → other block  (number 999) → check fails
//
// Result: Rust accepts, C VM rejects → miner produces invalid block
let mut header_deps = vec![other_block.hash()]; // position 0: number 999
header_deps.extend(vec![h256!("0x1").into(); 255]); // positions 1-255: dummies
header_deps.push(deposit_block.hash()); // position 256: number 100

let witness = WitnessArgs::new_builder()
    .input_type(Some(Bytes::from(256u64.to_le_bytes().to_vec())))
    .build();
// Cell data = 100 (deposit block number)
// Rust: header_deps[256].number() == 100 == cell_data → PASS
// C VM: header_deps[0].number()   == 999 != cell_data → FAIL
``` [8](#0-7) [6](#0-5)

### Citations

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
    }
```

**File:** util/dao/src/lib.rs (L38-42)
```rust
    fn transaction_maximum_withdraw(
        &self,
        rtx: &ResolvedTransaction,
    ) -> Result<Capacity, DaoError> {
        let header_deps: HashSet<Byte32> = rtx.transaction.header_deps_iter().collect();
```

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
