### Title
Missing DAO Header-Dep Index Range Validation Causes Rust/C-VM Discrepancy in Fee Calculation - (File: util/dao/src/lib.rs)

### Summary
The `transaction_maximum_withdraw` function in `util/dao/src/lib.rs` reads the `header_dep_index` from the witness as a full `u64` but never validates that it fits within a `u8` (≤ 255). The on-chain C VM (`dao.c`) reads only the **lowest byte** of this index. For any index > 255, the Rust fee calculator and the C VM silently resolve to **different** `header_deps` entries, creating a split: the C VM accepts the transaction while the Rust node rejects it.

### Finding Description
In `transaction_maximum_withdraw`, the witness `input_type` field is decoded as a little-endian `u64` and used directly to index `header_deps`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)   // full u64 used here
        ...
``` [1](#0-0) 

The on-chain `dao.c` script reads only the **lowest byte** of the same 8-byte field. For `header_dep_index = 257` (binary `0x0000000000000101`):

- **C VM** resolves `header_deps[1]` → deposit block (number 100) → block-number check passes → **accepts**
- **Rust** resolves `header_deps[257]` → withdraw block (number 200) → block-number check `200 ≠ 100` → **rejects**

The block-number guard at line 105 catches the mismatch on the Rust side:

```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
``` [2](#0-1) 

but no equivalent guard prevents the index from exceeding 255 in the first place. The test `check_dao_withdraw_header_dep_index_exceeds_u8` explicitly encodes this split: [3](#0-2) 

The same `DaoCalculator::transaction_fee` path is invoked in two security-critical locations:

1. **Tx-pool admission** via `check_tx_fee` in `tx-pool/src/util.rs`: [4](#0-3) 

2. **Block-level fee verification** via `FeeCalculator::transaction_fee` in `verification/src/transaction_verifier.rs`: [5](#0-4) 

### Impact Explanation
**Tx-pool denial of service (reachable by any RPC caller / tx-pool submitter):** Any DAO withdrawal transaction whose witness encodes `header_dep_index > 255` is permanently rejected by the Rust tx-pool even though the on-chain C VM would accept and execute it correctly. The user has no recourse through the standard `send_transaction` RPC path.

**Consensus split risk:** Because `FeeCalculator` (used inside `ContextualTransactionVerifier`) calls the same `DaoCalculator::transaction_fee`, a miner who directly assembles a block containing such a transaction would produce a block that the C VM considers valid but that Rust full nodes reject during block verification. This creates a chain-split condition between Rust nodes and any non-Rust implementation that faithfully follows the C VM semantics.

### Likelihood Explanation
The attacker-controlled entry path is the standard `send_transaction` RPC or the compact-block relay path. Crafting a DAO withdrawal with `header_dep_index = 257` requires only constructing a transaction with ≥ 258 `header_deps` entries and setting the witness `input_type` to `257u64` in little-endian. No privileged role is needed to craft the transaction; a miner is needed only for the consensus-split variant. The normal index range is 0–255, so accidental triggering is unlikely, but deliberate triggering is straightforward.

### Recommendation
Add an explicit range check immediately after decoding `header_dep_index`:

```rust
if header_dep_index > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
```

This aligns the Rust fee calculator with the C VM's lowest-byte semantics and eliminates the split. Alternatively, document and enforce that `header_deps` lists in DAO withdrawal transactions must contain fewer than 256 entries, and reject transactions that violate this at the tx-pool admission layer.

### Proof of Concept
1. Obtain a live DAO deposit cell whose cell data encodes `deposited_block_number = 100`.
2. Build a withdrawal transaction with 258 `header_deps` entries: position 1 = the deposit block (number 100), position 257 = the prepare/withdraw block (number 200), all other positions = dummy hashes.
3. Set `Witn

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

**File:** util/dao/src/tests.rs (L476-537)
```rust
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
