### Title
DAO Withdrawal `header_deps` Index Parsed as Full `u64` in Rust but as Lowest Byte in C VM, Causing Consensus Split and Tx-Pool DoS — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` in `util/dao/src/lib.rs` reads the deposit-header index from the witness as a full 8-byte little-endian `u64`, while the on-chain C VM DAO script reads only the **lowest byte** of that same 8-byte field. When a transaction encodes an index whose full `u64` value differs from its lowest byte (i.e., any index > 255), the Rust node and the C VM resolve different entries in `header_deps`, producing a consensus split. The Rust tx-pool rejects transactions that the C VM would accept, and can accept transactions that the C VM would reject.

---

### Finding Description

In `transaction_maximum_withdraw`, the deposit-header index is extracted from the witness `input_type` field as:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
``` [1](#0-0) 

This full `u64` value is then used directly to index into `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
``` [2](#0-1) 

The on-chain C VM DAO script, however, reads only the **lowest byte** of the same 8-byte little-endian field. This discrepancy is explicitly documented in the codebase's own test:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
``` [3](#0-2) 

For any witness index `N` where `N > 255` and `N & 0xFF != N`:

| Validator | Index read | `header_deps` entry used |
|---|---|---|
| C VM (on-chain) | `N & 0xFF` (lowest byte) | `header_deps[N & 0xFF]` |
| Rust `DaoCalculator` | full `u64` `N` | `header_deps[N]` |

These point to **different** entries, so the two validators operate on different deposit headers.

The `DaoCalculator` is invoked on every DAO withdrawal transaction entering the tx-pool via `check_tx_fee`:

```rust
let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
    .transaction_fee(rtx)
``` [4](#0-3) 

---

### Impact Explanation

**Split direction 1 — Rust rejects, C VM accepts (tx-pool DoS):**
A transaction sender places the correct deposit block at `header_deps[N & 0xFF]` and an unrelated block at `header_deps[N]`, then sets the witness index to `N`. The C VM resolves the correct deposit header and accepts the transaction. Rust resolves `header_deps[N]`, which has a different block number than the value stored in the cell data, triggering the block-number mismatch check at line 105:

```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
``` [5](#0-4) 

The Rust node permanently rejects a valid DAO withdrawal from the tx-pool. The depositor cannot reclaim their funds through this node.

**Split direction 2 — Rust accepts, C VM rejects (invalid block production):**
A transaction sender places the correct deposit block at `header_deps[N]` and an unrelated block at `header_deps[N & 0xFF]`. Rust resolves the correct deposit header and accepts the transaction. The C VM resolves the wrong header and rejects the script execution. If a miner includes this transaction in a block, the block fails script validation on all other nodes, causing the miner to produce an invalid block.

---

### Likelihood Explanation

The attack requires a DAO withdrawal transaction with at least 257 `header_deps` entries and a witness index > 255. While unusual in normal usage, this is a valid transaction structure with no protocol-level limit preventing it. A transaction sender (unprivileged) can craft and submit such a transaction directly via the `send_transaction` RPC. The discrepancy is already documented in the production codebase's own test, confirming the developers are aware the two parsers diverge.

---

### Recommendation

Change `transaction_maximum_withdraw` to read only the lowest byte of the 8-byte index field, matching the C VM DAO script's behavior:

```rust
// Before (reads full u64):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// After (reads only lowest byte, matching C VM):
Ok(header_deps_index_data.unwrap()[0] as u64)
```

Alternatively, add an explicit rejection of any index whose full `u64` value exceeds `u8::MAX`, so that the discrepancy is surfaced as a hard error rather than a silent consensus split.

---

### Proof of Concept

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the split:

- `header_deps[1]` = correct deposit block (block 100) — what the C VM resolves via lowest byte of `257`
- `header_deps[257]` = withdraw block (block 200) — what Rust resolves via full `u64` `257`
- Witness `input_type` = `257u64` in little-endian

The test asserts `result.is_err()` — Rust rejects the transaction — while the comment confirms the C VM would accept it using the lowest byte. [6](#0-5) 

The `check_tx_fee` call site in the tx-pool is the reachable entry point for an unprivileged transaction sender: [7](#0-6)

### Citations

**File:** util/dao/src/lib.rs (L91-99)
```rust
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

**File:** tx-pool/src/util.rs (L28-54)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
}
```
