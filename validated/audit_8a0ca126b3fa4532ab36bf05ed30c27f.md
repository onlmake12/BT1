### Title
`DaoCalculator` Reads Full u64 Header-Dep Index While On-Chain C DAO Script Reads Only the Lowest Byte — Consensus Split on DAO Withdrawal Transactions - (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` in `util/dao/src/lib.rs` resolves the deposit-block header by reading the full 8-byte little-endian u64 from the witness `input_type` field. The deployed C DAO script (`dao.c`) reads only the **lowest byte** of that same 8-byte field. For any witness index value whose lowest byte differs from its full u64 value (i.e., any index > 255), the two implementations select **different** header-dep entries. The Rust node therefore rejects DAO withdrawal transactions that the on-chain C script would accept, producing a consensus split.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the header-deps index from the witness as a full u64:

```rust
// line 91
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// line 96
.get(header_dep_index as usize)
```

The comment on line 79 says *"dao contract stores header deps index as u64 in the input_type field of WitnessArgs"*, but the actual deployed C DAO script (`dao.c`, referenced at `test/src/specs/dao/dao_user.rs:14`) reads only the **lowest byte** of that 8-byte field.

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` explicitly documents the discrepancy:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

With witness index = 257 (little-endian bytes: `0x01 0x01 0x00 0x00 0x00 0x00 0x00 0x00`):
- **C DAO script** reads lowest byte → index 1 → `deposit_block` → **accepts** the transaction
- **Rust `DaoCalculator`** reads full u64 → index 257 → `withdraw_block` → block-number check fails → **rejects** the transaction

The test asserts `result.is_err()`, confirming the Rust node rejects what the C script accepts.

`DaoCalculator::transaction_fee` is called in two production paths:
1. **tx-pool admission**: `tx-pool/src/util.rs` `check_tx_fee` (line 34–35)
2. **block verification**: `verification/src/transaction_verifier.rs` `FeeCalculator::transaction_fee` (line 270–271)

Both paths use the Rust u64 interpretation, so the split affects both tx-pool admission and block acceptance.

---

### Impact Explanation

A DAO depositor submits a valid phase-2 withdrawal transaction whose `header_deps` list has ≥ 258 entries and whose witness `input_type` encodes an index > 255 (e.g., 257). The C DAO script on-chain accepts the transaction because it resolves the deposit header correctly via the lowest byte. Every Rust CKB node rejects the transaction at the tx-pool level (`Reject::Malformed`) and, if the transaction were mined into a block by a non-Rust miner, would reject the entire block. This is a **consensus split** and a **permanent DoS** against the affected DAO depositor's funds: the withdrawal transaction is valid on-chain but unprocessable by Rust nodes.

---

### Likelihood Explanation

Constructing a transaction with 258 `header_deps` entries is unusual but entirely within protocol limits — `header_deps` is an unbounded `Byte32Vec`. A DAO user who builds their withdrawal transaction programmatically (e.g., via a custom wallet or script) and happens to reference many headers, or who deliberately crafts such a transaction, can trigger this path. No privileged access, key material, or majority hash power is required. The entry point is the standard `send_transaction` RPC or P2P relay.

---

### Recommendation

In `util/dao/src/lib.rs`, change the index read to match the C DAO script's actual behavior — read only the lowest byte:

```rust
// Before (line 91):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// After — match the C DAO script which reads only the lowest byte:
Ok(u64::from(header_deps_index_data.unwrap()[0]))
```

Alternatively, add a consensus-level validation that rejects any DAO withdrawal transaction whose witness index exceeds 255, so both the Rust node and the C script agree on the rejection boundary.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` is a direct proof of concept:

1. Build a DAO withdrawal `ResolvedTransaction` with 258 `header_deps`.
   - `header_deps[1]` = deposit block hash (block 100)
   - `header_deps[257]` = withdraw block hash (block 200)
2. Set witness `input_type` = `257u64.to_le_bytes()` (lowest byte = 1).
3. Set cell data = `100u64.to_le_bytes()` (deposited at block 100).
4. Call `DaoCalculator::transaction_fee(&rtx)`.

**Result**: Rust returns `Err(InvalidOutPoint)` because it resolves index 257 → block 200, whose number (200) ≠ deposited block number (100). The C DAO script would resolve index 1 → block 100, whose number (100) = deposited block number (100), and would accept the transaction. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
