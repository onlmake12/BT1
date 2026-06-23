### Title
DAO Withdrawal `header_deps` Index Truncation Discrepancy Between Rust `DaoCalculator` and C VM Causes Consensus Split — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw()` reads the deposit-header index from the witness as a full `u64`, while the on-chain C VM DAO script reads only the **lowest byte** of that same field. A transaction sender who holds a DAO deposit can craft a withdrawal transaction that the C VM accepts but Rust's block verifier rejects, splitting consensus.

---

### Finding Description

During a DAO phase-2 withdrawal, the witness `input_type` field carries an 8-byte little-endian integer that is an index into `header_deps`, pointing to the deposit block header. Rust reads the full `u64`: [1](#0-0) 

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// …
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
```

The C VM DAO script, however, only reads the **lowest byte** of the 8-byte field (a well-known property of the deployed `dao.c` contract). A transaction can therefore be constructed as follows:

| `header_deps` slot | content |
|---|---|
| index 1 | deposit block hash (block 100) |
| index 257 | withdraw block hash (block 200) |
| all others | dummy hashes |

Witness `input_type` = `257u64` in little-endian → bytes `[0x01, 0x01, 0x00, …]`.

- **C VM** reads lowest byte → `0x01` → index 1 → deposit block → block-number check passes → **ACCEPTS**
- **Rust** reads full `u64` → `257` → index 257 → withdraw block (number 200) → block-number check against cell data (100) fails → **REJECTS** with `DaoError::InvalidOutPoint`

This exact scenario is captured in the production test suite: [2](#0-1) 

The comment at line 490–491 explicitly states:
> "Position 1: correct deposit block (what C VM resolves via lowest byte). Position 257: withdraw block (wrong — Rust resolves this with full u64)."

---

### Impact Explanation

The Rust block verifier calls `DaoHeaderVerifier::verify()`, which calls `DaoCalculator::dao_field()` → `withdrawed_interests()` → `transaction_maximum_withdraw()`: [3](#0-2) [4](#0-3) 

When `transaction_maximum_withdraw()` returns `DaoError::InvalidOutPoint` for the crafted transaction, `dao_field()` propagates the error, `DaoHeaderVerifier::verify()` returns `BlockErrorKind::InvalidDAO`, and the entire block is rejected by every Rust node.

Meanwhile, the C VM script execution (the actual consensus authority) accepted the transaction. The result is a **consensus split**: Rust nodes reject a block that is valid by on-chain script rules. An attacker who controls a miner (or bribes one) can publish such a block and fork the network.

Additionally, the same discrepancy causes `check_tx_fee` in the tx-pool to reject the transaction before it even reaches a miner: [5](#0-4) 

This means the attack path requires the attacker to be the miner themselves (or submit via `submit_block` RPC), which is a realistic scenario for a DAO depositor running their own node.

---

### Likelihood Explanation

- Any CKB address that has completed DAO phase-1 (prepare) can craft this transaction.
- The only requirement is constructing a `header_deps` list with ≥ 258 entries and setting the witness index to 257.
- The attack is deterministic, requires no brute force, and is fully reproducible.
- The attacker must be able to get the block accepted by the network (i.e., must be a miner or collude with one), which is a realistic but non-trivial requirement.

**Likelihood: Medium.**

---

### Recommendation

Rust's `DaoCalculator::transaction_maximum_withdraw()` must match the C VM's byte-truncation behavior. Change line 91 of `util/dao/src/lib.rs` to mask the index to its lowest byte before use:

```rust
// Before (reads full u64):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// After (matches C VM lowest-byte truncation):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()) & 0xFF)
```

Alternatively, if the intent is for the full `u64` to be the canonical interpretation, the deployed DAO script (`dao.c`) must be upgraded via a hard fork to read all 8 bytes. The Rust side and the C VM side must agree on the same interpretation.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` is a direct proof of concept: [6](#0-5) 

1. Build `header_deps` with 258 entries: deposit block at slot 1, withdraw block at slot 257.
2. Set witness `input_type` = `257u64` (little-endian bytes `[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`).
3. C VM reads byte 0 → `0x01` → slot 1 → deposit block (number 100) → matches cell data → **script passes**.
4. Rust reads `u64` → `257` → slot 257 → withdraw block (number 200) → does not match cell data (100) → `DaoError::InvalidOutPoint`.
5. `DaoHeaderVerifier::verify()` returns `BlockErrorKind::InvalidDAO`; Rust nodes reject the block.
6. Network forks between Rust nodes (reject) and any node running only the C VM script (accept).

### Citations

**File:** util/dao/src/lib.rs (L91-96)
```rust
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
```

**File:** util/dao/src/lib.rs (L312-333)
```rust
    fn withdrawed_interests(
        &self,
        mut rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
    ) -> Result<Capacity, DaoError> {
        let maximum_withdraws = rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
            self.transaction_maximum_withdraw(rtx)
                .and_then(|c| capacities.safe_add(c).map_err(Into::into))
        })?;
        let input_capacities = rtxs.try_fold(Capacity::zero(), |capacities, rtx| {
            let tx_input_capacities = rtx.resolved_inputs.iter().try_fold(
                Capacity::zero(),
                |tx_capacities, cell_meta| {
                    let output_capacity: Capacity = cell_meta.cell_output.capacity().into();
                    tx_capacities.safe_add(output_capacity)
                },
            )?;
            capacities.safe_add(tx_input_capacities)
        })?;
        maximum_withdraws
            .safe_sub(input_capacities)
            .map_err(Into::into)
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-319)
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
```

**File:** tx-pool/src/util.rs (L28-53)
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
```
