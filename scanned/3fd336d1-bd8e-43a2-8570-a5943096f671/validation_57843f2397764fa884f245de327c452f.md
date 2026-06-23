### Title
`DaoCalculator::transaction_maximum_withdraw` Reads Header Dep Index as Full u64 While On-Chain dao.c Reads Only the Lowest Byte, Creating a Consensus Split - (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` resolves the deposit header by reading the witness `input_type` field as a full 8-byte little-endian `u64` index into `header_deps`. The on-chain C script (`dao.c`) reads only the **lowest byte** of that same 8-byte field. This mismatch means the Rust verifier and the on-chain VM use a different `header_deps` slot to identify the deposit block, producing two distinct consensus-split directions that are both reachable by an unprivileged transaction sender.

---

### Finding Description

In `DaoCalculator::transaction_maximum_withdraw`, the deposit header is located by parsing the witness `input_type` field:

```rust
// util/dao/src/lib.rs  line 91
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

This reads all 8 bytes as a `u64`, then uses it as `header_deps[index]`.

The on-chain `dao.c` script reads only the lowest byte of the same 8-byte field (treating it as a `uint8_t` index). The discrepancy is explicitly documented in the test suite:

```
// util/dao/src/tests.rs  lines 489-491
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

The test asserts `result.is_err()` for witness index 257, confirming the Rust verifier resolves a different slot than the C VM.

`transaction_fee` (which calls `transaction_maximum_withdraw`) is invoked in two critical paths:

1. **Tx-pool admission** — `check_tx_fee` in `tx-pool/src/util.rs` line 34–41
2. **Block-level contextual verification** — `FeeCalculator::transaction_fee` in `verification/src/transaction_verifier.rs` line 265–273, called from `ContextualTransactionVerifier::verify` at line 170

---

### Impact Explanation

**Direction A — Rust rejects, C VM accepts (DoS against DAO withdrawers)**

Craft a valid DAO withdrawal where the witness `input_type` encodes index `257` (bytes `0x01 0x01 0x00 … 0x00`):

| Slot | Content |
|------|---------|
| `header_deps[1]` | deposit block hash (C VM resolves: lowest byte = 1) |
| `header_deps[257]` | withdraw block hash (Rust resolves: full u64 = 257) |

- C VM: reads byte 0 of the 8-byte field → index 1 → deposit block → block-number check passes → **ACCEPT**
- Rust: reads full u64 → index 257 → withdraw block → block-number mismatch → `DaoError::InvalidOutPoint` → **REJECT**

The Rust tx-pool rejects the transaction. A legitimate user whose wallet constructs a witness index above 255 cannot withdraw DAO funds through any standard CKB node.

**Direction B — Rust accepts, C VM rejects (invalid block production / miner DoS)**

Craft a transaction where witness `input_type` encodes index `256` (bytes `0x00 0x01 0x00 … 0x00`):

| Slot | Content |
|------|---------|
| `header_deps[0]` | dummy / wrong block hash (C VM resolves: lowest byte = 0) |
| `header_deps[256]` | deposit block hash (Rust resolves: full u64 = 256) |

- Rust: reads full u64 → index 256 → deposit block → block-number matches → `transaction_fee` returns `Ok` → **tx-pool admits the transaction**
- C VM: reads lowest byte → index 0 → wrong block → block-number mismatch → script fails → **REJECT**

When a miner includes this transaction in a block, `script.verify()` fails, making the block invalid. The miner's PoW work is wasted. An attacker can flood the tx-pool with such transactions to continuously invalidate mined blocks.

---

### Likelihood Explanation

Any unprivileged transaction sender can submit a DAO withdrawal transaction to the RPC (`send_transaction`) or relay it over P2P. No special privilege, key, or majority hashpower is required. Constructing a witness with a multi-byte index is trivial. The discrepancy is already documented in the test suite, confirming the code path is reachable and the behavior is reproducible.

---

### Recommendation

Change the index parsing in `transaction_maximum_withdraw` to read only the lowest byte, matching the on-chain C script:

```diff
- Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
+ Ok(header_deps_index_data.unwrap()[0] as u64)
```

Alternatively, add an explicit bounds check rejecting any witness whose `input_type` encodes a value greater than `u8::MAX`, so the Rust verifier and the C VM are guaranteed to agree on the resolved slot.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–537) already demonstrates Direction A: it constructs a transaction with witness index 257, places the deposit block at `header_deps[1]` (the C VM's slot) and the withdraw block at `header_deps[257]` (the Rust slot), and asserts `result.is_err()`.

Direction B can be demonstrated by mirroring the same test with index 256, placing the deposit block at `header_deps[256]` and a dummy block at `header_deps[0]`. The Rust `transaction_fee` call returns `Ok` (deposit block number matches at slot 256), while the C VM would read slot 0 (dummy block), fail the block-number check, and reject the script — causing any block containing the transaction to be invalid. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** verification/src/transaction_verifier.rs (L162-171)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
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
