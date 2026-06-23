### Title
`DaoCalculator` Reads Full u64 Header-Dep Index While C VM Reads Only Lowest Byte, Causing Valid DAO Withdrawal Transactions to Be Permanently Rejected from Tx-Pool — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` reads the 8-byte `input_type` witness field as a full `u64` and uses it directly to index into `header_deps`. The on-chain C VM implementation of the DAO type script reads only the **lowest byte** of that same 8-byte field. When a transaction carries a witness index whose full u64 value exceeds 255 but whose lowest byte correctly identifies the deposit block, the C VM accepts the transaction while the Rust tx-pool fee-check rejects it. The transaction is permanently blocked from the tx-pool even though it is consensus-valid.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the header-dep index from the witness `input_type` field:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

and immediately uses it as an array subscript:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
    .and_then(|hash| header_deps.get(&hash))
    .ok_or(DaoError::InvalidOutPoint)
```

The on-chain DAO C script reads only the lowest byte of the same 8-byte field (treating it as a `uint8_t` index). When a transaction sets `input_type` to a value such as `257` (little-endian `0x01 0x01 0x00 …`):

- **C VM** reads byte 0 → index `1` → `header_deps[1]` = deposit block → **accepts**
- **Rust** reads full u64 → index `257` → `header_deps[257]` = a different block → block-number check fails → **rejects**

The tx-pool admission path calls `check_tx_fee` → `DaoCalculator::transaction_fee` on every submitted transaction. A rejection here surfaces as `Reject::Malformed`, permanently excluding the transaction from the pool.

The repository's own test documents this exact divergence:

```
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

and asserts `result.is_err()` — confirming Rust rejects what the C VM accepts.

---

### Impact Explanation

Any DAO withdrawal transaction whose `WitnessArgs.input_type` encodes an index `N > 255` where `N & 0xFF` correctly identifies the deposit block in `header_deps` will be:

1. **Accepted by consensus** (C VM reads lowest byte → correct deposit block).
2. **Permanently rejected by every node's tx-pool** (Rust reads full u64 → wrong or absent block → `DaoError::InvalidOutPoint` → `Reject::Malformed`).

The user cannot withdraw their DAO deposit through any standard node. The funds are not lost (the transaction could theoretically be included directly in a block by a miner who bypasses the tx-pool), but the normal submission path is irreversibly broken for that transaction shape.

---

### Likelihood Explanation

A transaction with 256+ `header_deps` is unusual but not prohibited by consensus. A user withdrawing many DAO cells from different deposit blocks, or a script that deliberately pads `header_deps`, can reach this state. The witness index field is 8 bytes wide and the protocol never validates that its value fits in a `u8`, so any value in `[256, u64::MAX]` whose lowest byte is a valid deposit-block index triggers the mismatch. An unprivileged RPC caller (`send_transaction`) is the entry point; no special privilege is required.

---

### Recommendation

In `transaction_maximum_withdraw`, after reading the raw u64, validate that it fits in a `u8` (matching the C VM's interpretation) and return `DaoError::InvalidDaoFormat` if it does not:

```rust
let raw_index = LittleEndian::read_u64(&header_deps_index_data.unwrap());
if raw_index > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
let header_dep_index = raw_index as usize;
```

Alternatively, align the Rust implementation with the C VM by reading only the first byte of `header_deps_index_data` as the index. Either fix makes the Rust fee calculator consistent with the on-chain script.

---

### Proof of Concept

The repository's own test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` demonstrates the divergence: [1](#0-0) 

The root cause is the unconstrained u64 read followed by direct array indexing: [2](#0-1) 

The tx-pool fee check that triggers this path on every submitted transaction: [3](#0-2) 

Called from the tx-pool pre-check admission gate: [4](#0-3)

### Citations

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

**File:** tx-pool/src/process.rs (L286-291)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
```
