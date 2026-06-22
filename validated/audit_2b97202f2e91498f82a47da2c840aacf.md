### Title
DAO Withdrawal `header_dep_index` Interpretation Discrepancy Between Rust `DaoCalculator` and On-Chain C VM — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` reads the `header_dep_index` stored in the DAO withdrawal witness as a full 8-byte little-endian `u64`, while the on-chain C VM DAO script (`dao.c`) reads only the **lowest byte** of that same field. For any index value > 255, the two interpretations diverge. The Rust tx-pool rejects a transaction that the on-chain script would accept, creating a discrepancy analogous to the ETH/WETH fallthrough in the Furo report: one path (Rust) silently routes to a different header than the other path (C VM), leaving the transaction in an inconsistent state between layers.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` parses the deposit header index from the witness `input_type` field:

```rust
// dao contract stores header deps index as u64 in the input_type field of WitnessArgs
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then uses this full `u64` to index into `header_deps`:

```rust
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)   // full u64 cast to usize
        ...
})
```

The on-chain `dao.c` script, however, reads only the **lowest byte** of the same 8-byte field. This is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs`:

```
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

With `header_dep_index = 257` (little-endian bytes: `0x01 0x01 0x00 ...`):
- **C VM** reads lowest byte → index `1` → `header_deps[1]` = deposit block → `deposit_header.number() == deposited_block_number` → **ACCEPT**
- **Rust** reads full u64 → index `257` → `header_deps[257]` = withdraw block → `deposit_header.number() (200) != deposited_block_number (100)` → `DaoError::InvalidOutPoint` → **REJECT**

The test asserts `result.is_err()`, confirming the Rust path rejects what the C VM accepts.

This rejection propagates through `check_tx_fee` in `tx-pool/src/util.rs`:

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

Any DAO withdrawal transaction with `header_dep_index > 255` is rejected at tx-pool admission with `Reject::Malformed`, even though the on-chain script would execute successfully.

Additionally, `DaoCalculator::dao_field` (used during block assembly) calls `transaction_maximum_withdraw` for all transactions in the block. If a miner bypasses the tx-pool and includes such a transaction, the `dao_field` written into the block header would be computed using the wrong deposit header (Rust's interpretation), producing an incorrect accumulation-rate field. Nodes verifying the block's DAO field would reject it, while nodes that only run the C VM script would accept it — a consensus split.

---

### Impact Explanation

**Primary — Tx-pool DoS on legitimate DAO withdrawals**: Any DAO depositor whose withdrawal transaction encodes `header_dep_index > 255` (e.g., due to a large `header_deps` list) cannot submit through the standard tx-pool path. The transaction is permanently rejected with `Reject::Malformed` despite being valid on-chain.

**Secondary — Consensus split via incorrect `dao_field`**: A miner who includes such a transaction directly in a block (bypassing the tx-pool) will compute an incorrect `dao_field` in the block header using the wrong deposit header. Nodes that verify the `dao_field` (using Rust's `DaoCalculator`) will reject the block; nodes that only run the C VM script will accept it. This splits consensus.

---

### Likelihood Explanation

The discrepancy is triggered whenever `header_dep_index > 255`. In normal DAO usage the index is 0 or 1, so accidental triggering is rare. However, an attacker who understands the discrepancy can deliberately craft a withdrawal transaction with 258+ `header_deps` and set `header_dep_index = 257` (lowest byte = 1, pointing to the correct deposit block). This requires only a valid DAO deposit and the ability to submit a transaction — no privileged access. The attacker-controlled entry path is the standard `send_transaction` RPC or direct P2P relay.

---

### Recommendation

Align the Rust `DaoCalculator` with the on-chain C VM behavior: read only the lowest byte of `header_dep_index` (i.e., `header_deps_index_data.unwrap()[0] as usize`), or add an explicit validation step that rejects any `header_dep_index > 255` before the index is used, so both layers agree on which inputs are invalid. The fix must be applied consistently in `transaction_maximum_withdraw` and anywhere else `DaoCalculator` parses this field.

---

### Proof of Concept

The discrepancy is directly demonstrated by the existing test in `util/dao/src/tests.rs`:

1. Build a DAO withdrawal transaction with 258 `header_deps`, `header_deps[1]` = deposit block, `header_deps[257]` = withdraw block, and witness `input_type = 257u64.to_le_bytes()`.
2. Call `DaoCalculator::transaction_fee(&rtx)`.
3. Rust reads index 257 → withdraw block (number 200); cell data says deposited at block 100; `deposit_header.number() != deposited_block_number` → `Err(DaoError::InvalidOutPoint)`.
4. The C VM reads lowest byte = 1 → deposit block (number 100); block number matches → script passes.
5. The same transaction is accepted on-chain but rejected by the Rust tx-pool via `check_tx_fee` → `Reject::Malformed`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** util/dao/src/lib.rs (L58-66)
```rust
                    if is_dao_output {
                        // A withdrawing DAO cell has 8 bytes of cell data storing the
                        // block number of the original deposit.
                        let deposited_block_number =
                            match self.data_loader.load_cell_data(cell_meta) {
                                Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
                                _ => 0,
                            };
                        if deposited_block_number > 0 {
```

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

**File:** util/dao/src/tests.rs (L489-536)
```rust
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
