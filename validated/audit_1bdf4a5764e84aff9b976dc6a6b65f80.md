### Title
DAO Withdrawal `header_dep_index` Interpretation Mismatch Between Rust Fee Calculator and C VM Script Causes Consensus Split — (`File: util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw()` reads the `header_dep_index` from the witness `input_type` field as a full `u64`, while the on-chain C VM `dao.c` script reads only the **lowest byte** of the same 8-byte value. When a DAO withdrawal transaction encodes `header_dep_index >= 256`, the Rust node and the C VM resolve different entries in `header_deps[]`. The C VM script passes, but the Rust `FeeCalculator` (called inside `ContextualTransactionVerifier::verify()`) returns `DaoError::InvalidOutPoint`, causing the Rust node to reject the block. This is a consensus split: a block valid per on-chain script execution is rejected by the Rust node's fee accounting layer.

---

### Finding Description

**Root cause — `util/dao/src/lib.rs`, lines 91–98:**

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)   // full u64 used as index
        .and_then(|hash| header_deps.get(&hash))
        .ok_or(DaoError::InvalidOutPoint)
})?;
```

The Rust code reads the full 8-byte little-endian `u64` from the witness and uses it directly as the index into `header_deps`. The on-chain `dao.c` script, however, interprets only the **lowest byte** of the same 8-byte field as the index (i.e., `index & 0xFF`).

This discrepancy is explicitly documented in the test at `util/dao/src/tests.rs` lines 489–495:

```rust
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
let dummy = h256!("0x1").into();
let mut header_deps = vec![dummy; 258];
header_deps[1] = deposit_block.hash();
header_deps[257] = withdraw_block.hash();
```

For `header_dep_index = 257` (bytes `[0x01, 0x01, 0x00, ...]`):
- **C VM** reads lowest byte → index `1` → `deposit_block` (block 100) → matches cell data → **script passes**
- **Rust** reads full u64 → index `257` → `withdraw_block` (block 200) → `deposit_header.number() != deposited_block_number` (200 ≠ 100) → **`DaoError::InvalidOutPoint`**

**The fee calculator is called inside block verification.** In `verification/src/transaction_verifier.rs` lines 162–171:

```rust
pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
    self.time_relative.verify()?;
    self.capacity.verify()?;
    let cycles = ... self.script.verify(max_cycles)?;  // C VM passes
    let fee = self.fee_calculator.transaction_fee()?;  // Rust rejects here
    Ok(Completed { cycles, fee })
}
```

`ContextualTransactionVerifier::verify()` is invoked by `BlockTxsVerifier` in `verification/contextual/src/contextual_block_verifier.rs` lines 426–443 during block validation. The C VM script executes and passes first; then the Rust `FeeCalculator` calls `DaoCalculator::transaction_fee()`, which calls `transaction_maximum_withdraw()`, which fails with `InvalidOutPoint`. The block is rejected.

---

### Impact Explanation

1. **Consensus split**: A block containing a DAO withdrawal with `header_dep_index >= 256` passes C VM script execution but is rejected by the Rust node's `ContextualTransactionVerifier`. Any miner who assembles such a block loses their block reward and the block is orphaned by all Rust nodes.

2. **Tx-pool denial**: `tx-pool/src/util.rs` line 34–41 calls `DaoCalculator::transaction_fee()` via `check_tx_fee()`. Any DAO withdrawal with `header_dep_index >= 256` is rejected at tx-pool admission with `Reject::Malformed`, even though the transaction is valid per the C VM.

3. **Miner griefing**: An attacker can submit a crafted DAO withdrawal to a miner's RPC (bypassing the tx-pool via `submit_block` or direct block assembly), causing the miner to produce an invalid block and lose the block reward.

---

### Likelihood Explanation

An unprivileged transaction sender can craft a DAO withdrawal transaction with `header_dep_index = 257` (or any value ≥ 256 whose lowest byte points to the correct deposit block hash position). The `header_deps` array is fully attacker-controlled. No privileged access, key material, or majority hashpower is required. The attacker only needs to submit the crafted transaction to a miner's node or include it in a block template. The entry path is the standard `send_transaction` RPC or direct block assembly.

---

### Recommendation

In `util/dao/src/lib.rs`, the `header_dep_index` read from the witness must be validated to fit within a `u8` (i.e., `<= 255`) before use, to match the C VM's behavior. Alternatively, the index should be masked to its lowest byte (`header_dep_index & 0xFF`) to align with the on-chain script's interpretation. The fix must be applied consistently in both `transaction_maximum_withdraw()` and any other Rust-side DAO index resolution logic.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` lines 475–537 directly demonstrates the discrepancy:

- `header_dep_index = 257` is encoded in the witness
- `header_deps[1]` = deposit block (block 100) — what the C VM resolves
- `header_deps[257]` = withdraw block (block 200) — what Rust resolves
- The test asserts `result.is_err()` (Rust rejects), while the C VM would accept (lowest byte = 1 → deposit block → block number matches cell data)

A concrete attack:
1. Deposit CKB into NervosDAO normally.
2. Construct a Phase 1 (prepare) transaction normally.
3. Construct a Phase 2 (withdraw) transaction with `header_deps` padded to 258 entries: deposit block hash at index 1, withdraw block hash at index 257. Set witness `input_type` = `257u64.to_le_bytes()`.
4. Submit this transaction directly to a miner's block assembler (bypassing the tx-pool).
5. The miner produces a block; the C VM script passes; the Rust `ContextualTransactionVerifier` rejects the block at `fee_calculator.transaction_fee()?`; the block is orphaned. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** verification/contextual/src/contextual_block_verifier.rs (L426-456)
```rust
                    ContextualTransactionVerifier::new(
                        Arc::clone(tx),
                        Arc::clone(&self.context.consensus),
                        self.context.store.as_data_loader(),
                        Arc::clone(&tx_env),
                    )
                    .verify(
                        self.context.consensus.max_block_cycles(),
                        skip_script_verify,
                    )
                    .map_err(|error| {
                        BlockTransactionsError {
                            index: index as u32,
                            error,
                        }
                        .into()
                    })
                    .map(|completed| (wtx_hash, completed))
                }.and_then(|result| {
                    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
                        DaoScriptSizeVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                        ).verify()?;
                    }
                    Ok(result)
                })
            })
            .skip(1) // skip cellbase tx
            .collect::<Result<Vec<(Byte32, Completed)>, Error>>()?;
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
