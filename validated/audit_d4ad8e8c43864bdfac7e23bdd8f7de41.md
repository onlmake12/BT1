### Title
`DaoCalculator` reads `header_deps_index` as full u64 while C VM uses only the lowest byte — (`File: util/dao/src/lib.rs`)

### Summary

`DaoCalculator::transaction_maximum_withdraw` in `util/dao/src/lib.rs` decodes the DAO withdrawal `header_deps_index` from the witness as a full little-endian u64. The on-chain C VM (`dao.c`) reads only the lowest byte of that same 8-byte field. When the stored index is ≥ 256, the two implementations resolve to different entries in `header_deps`, breaking fee accounting and DAO-field calculation in the Rust node.

### Finding Description

During a Nervos DAO phase-2 withdrawal, the transaction witness carries an 8-byte `input_type` field inside `WitnessArgs`. The C VM interprets this as a 1-byte index into `header_deps` (lowest byte only). The Rust `DaoCalculator` reads the full 8-byte little-endian u64:

```rust
// util/dao/src/lib.rs line 91
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// …
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // uses full u64
```

The existing unit test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` explicitly documents this split:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

With `header_deps_index = 257` (little-endian bytes `[0x01, 0x01, 0x00, …]`):
- C VM: `257 & 0xFF = 1` → `header_deps[1]` = deposit block → **valid**
- Rust: `257` → `header_deps[257]` = withdraw block → block-number mismatch → **error**

`DaoCalculator::transaction_fee` is called in two production paths:

1. **Tx-pool admission** — `tx-pool/src/util.rs:check_tx_fee` calls `DaoCalculator::transaction_fee` and rejects the transaction on error.
2. **Block assembly** — `tx-pool/src/block_assembler/mod.rs:calc_dao` calls `DaoCalculator::dao_field_with_current_epoch`, which internally calls `withdrawed_interests → transaction_maximum_withdraw`. A wrong index causes a wrong `withdrawed_interests` value, producing an incorrect DAO field committed into the block header.

### Impact Explanation

**Scenario A — DoS on valid DAO withdrawals via RPC:**
A user crafts a phase-2 DAO withdrawal with ≥ 258 `header_deps` and sets `header_deps_index = 257` (lowest byte = 1 = deposit block). The C VM accepts this transaction. The Rust node's `check_tx_fee` calls `DaoCalculator`, resolves index 257 to the wrong block, gets a block-number mismatch, and rejects the transaction with `Reject::Malformed`. The user's valid withdrawal is permanently blocked from entering the tx-pool.

**Scenario B — Incorrect DAO field / consensus split:**
A miner assembles a block containing such a withdrawal (e.g., received via a non-standard relay path). `calc_dao` calls `DaoCalculator::dao_field_with_current_epoch`; the Rust code computes a wrong `withdrawed_interests` because it resolves the wrong deposit header, producing an incorrect `s` field in the DAO data committed to the block header. Nodes that re-derive the DAO field from the C VM's perspective will compute a different value and reject the block, causing a consensus split.

### Likelihood Explanation

Any unprivileged transaction sender can trigger Scenario A by constructing a DAO withdrawal with more than 256 `header_deps` and placing the deposit block hash at position `(index & 0xFF)` while encoding a full u64 index ≥ 256 in the witness. No special privilege, key, or majority hash-power is required. Scenario B requires the transaction to reach a miner outside the standard Rust tx-pool path, which is realistic given that miners can accept transactions through custom endpoints or direct P2P relay.

### Recommendation

Align the Rust index decoding with the C VM's behavior. Either:
- Truncate the decoded index to its lowest byte before use: `(LittleEndian::read_u64(&data) & 0xFF) as usize`, or
- Validate that the stored u64 fits in a u8 and return `DaoError::InvalidDaoFormat` if it does not, matching the C VM's effective range.

Also add a consensus-level limit on `header_deps` length to prevent the ambiguity from arising in the first place.

### Proof of Concept

The existing test in `util/dao/src/tests.rs` already demonstrates the discrepancy:

```rust
// util/dao/src/tests.rs  lines 489–536
let mut header_deps = vec![dummy; 258];
header_deps[1] = deposit_block.hash();   // C VM resolves here (byte 0x01)
header_deps[257] = withdraw_block.hash(); // Rust resolves here (u64 = 257)

let witness = WitnessArgs::new_builder()
    .input_type(Some(Bytes::from(257u64.to_le_bytes().to_vec())))
    .build();
// …
let result = calculator.transaction_fee(&rtx);
// Rust: index 257 → withdraw block (number 200) ≠ deposited block (100) → Err
// C VM: index 257 & 0xFF = 1 → deposit block (number 100) == 100 → Ok
assert!(result.is_err(), "expected Err, got {result:?}");
```

A real attacker submits this transaction via `send_transaction` RPC. The Rust node's `check_tx_fee` hits the same code path and rejects the transaction with `Malformed`, while any node running the C VM directly would accept and propagate it. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** tx-pool/src/block_assembler/mod.rs (L676-679)
```rust
        // Generate DAO fields here
        let dao = DaoCalculator::new(consensus, &snapshot.borrow_as_data_loader())
            .dao_field_with_current_epoch(entries_iter, tip_header, current_epoch)?;

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
