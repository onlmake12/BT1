### Title
`DaoCalculator::transaction_maximum_withdraw` reads `header_dep_index` as full `u64` while on-chain `dao.c` reads only the lowest byte — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` interprets the `input_type` field of `WitnessArgs` as a full 8-byte little-endian `u64` index into `header_deps`. The on-chain `dao.c` script running inside CKB-VM reads only the **lowest byte** of that same field. When a DAO withdrawal transaction carries a `header_dep_index > 255`, the two sides resolve different entries in `header_deps`, causing the Rust node to reject transactions and blocks that the on-chain script accepts — a consensus split.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit-header index from the witness:

```rust
// dao contract stores header deps index as u64 in the input_type field of WitnessArgs
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

and then uses it directly:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
``` [1](#0-0) 

The on-chain `dao.c` script, however, reads only the **lowest byte** of the same 8-byte field (i.e., it treats the index as `u8`). This is documented explicitly in the test added to the codebase:

```
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
``` [2](#0-1) 

With `input_type = 257` (bytes `0x01 0x01 0x00 0x00 0x00 0x00 0x00 0x00`):
- **C VM (dao.c):** reads lowest byte → index `1` → deposit block → script succeeds
- **Rust DaoCalculator:** reads full u64 → index `257` → withdraw block → block-number check fails → `DaoError::InvalidOutPoint` [3](#0-2) 

---

### Impact Explanation

`DaoCalculator::transaction_fee` (which calls `transaction_maximum_withdraw`) is invoked in three critical paths:

1. **Tx-pool admission** (`tx-pool/src/util.rs`, `check_tx_fee`): a valid DAO withdrawal with `header_dep_index > 255` is rejected with `Reject::Malformed`, preventing it from ever being relayed or mined by this node. [4](#0-3) 

2. **Contextual transaction verification** (`verification/src/transaction_verifier.rs`, `FeeCalculator::transaction_fee`): the same mismatch causes `ContextualTransactionVerifier::verify` to return an error, so the node rejects the transaction even when processing a received block. [5](#0-4) 

3. **Block DAO-field verification** (`verification/contextual/src/contextual_block_verifier.rs`, `DaoHeaderVerifier::verify`): `dao_field` calls `withdrawed_interests` → `transaction_maximum_withdraw`. With the wrong header resolved, the computed DAO accumulation-rate field differs from the one the miner embedded, so the Rust node returns `BlockErrorKind::InvalidDAO` and rejects the entire block — a **consensus split**. [6](#0-5) 

---

### Likelihood Explanation

An unprivileged transaction sender can craft a DAO withdrawal transaction with ≥ 258 `header_deps` (each 32 bytes; 258 × 32 = 8 256 bytes, well within block limits), set `input_type = 257`, place the deposit block hash at index 1, and place an unrelated hash at index 257. The on-chain `dao.c` script accepts the transaction; every Rust CKB node rejects it and any block containing it. No privileged access, key material, or majority hash-power is required.

---

### Recommendation

Align the Rust `DaoCalculator` with the on-chain `dao.c` behavior: read the `header_dep_index` as a single byte (`u8`) rather than a full `u64`, or — if the intent is to support `u64` indices — update `dao.c` to read the full 8-byte value. Whichever interpretation is canonical must be consistent between the on-chain script and the off-chain Rust verifier.

In `util/dao/src/lib.rs`, change:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

to read only the lowest byte (matching `dao.c`):

```rust
Ok(header_deps_index_data.unwrap()[0] as u64)
``` [7](#0-6) 

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` already demonstrates the split: it constructs a transaction where the C VM would resolve index 1 (deposit block, block number 100) but the Rust `DaoCalculator` resolves index 257 (withdraw block, block number 200), causing `transaction_fee` to return `Err`. A block containing such a transaction, mined by a node whose `dao.c` accepted it, would be rejected by any Rust CKB node with `InvalidDAO`. [8](#0-7)

### Citations

**File:** util/dao/src/lib.rs (L79-96)
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-320)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao = DaoCalculator::new(
            &self.context.consensus,
            &self.context.store.borrow_as_data_loader(),
        )
        .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
        .map_err(|e| {
            error_target!(
                crate::LOG_TARGET,
                "Error generating dao data for block {}: {:?}",
                self.header.hash(),
                e
            );
            e
        })?;

        if dao != self.header.dao() {
            return Err((BlockErrorKind::InvalidDAO).into());
        }
        Ok(())
    }
```
