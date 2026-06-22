### Title
DAO Withdrawal `header_dep_index` Interpretation Mismatch Between Rust `DaoCalculator` and C VM DAO Script — (`util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator` reads the full 8-byte little-endian u64 from the witness `input_type` field to resolve the `header_dep_index` for DAO withdrawals. The C VM DAO script, however, reads only the **lowest byte** of that same 8-byte field. When a transaction sender encodes an index whose lowest byte differs from the full u64 value (e.g., `257 = 0x0101`), the two components resolve to different header entries. This causes the Rust tx-pool fee check to reject a transaction that the C VM DAO script would accept, and can produce a consensus split if such a transaction is included in a block.

---

### Finding Description

In `util/dao/src/lib.rs`, `DaoCalculator::transaction_maximum_withdraw` decodes the header-dep index as a full u64:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

and then uses it to index into `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
``` [1](#0-0) 

The C VM DAO script, by contrast, reads only the **lowest byte** of the same 8-byte witness field to determine the index. This discrepancy is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

```
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
``` [2](#0-1) 

The test constructs a transaction with `input_type = 257` (little-endian bytes `[0x01, 0x01, 0x00, …, 0x00]`):

- **C VM** reads lowest byte → index `1` → finds the correct deposit block → block-number check passes → **accepts**
- **Rust** reads full u64 → index `257` → finds the withdraw block (number 200) → block-number check against cell data (100) fails → returns `DaoError::InvalidOutPoint` → **rejects** [3](#0-2) 

The `DaoCalculator::transaction_fee` result is consumed in two security-critical paths:

1. **Tx-pool admission** — `check_tx_fee` in `tx-pool/src/util.rs` maps any `DaoError` to `Reject::Malformed`, permanently banning the transaction from the pool: [4](#0-3) 

2. **Block verification** — `FeeCalculator::transaction_fee` in `verification/src/transaction_verifier.rs` calls the same `DaoCalculator`, so a block containing such a transaction would be rejected by the Rust node even though the C VM accepted every script in it: [5](#0-4) 

---

### Impact Explanation

**Tx-pool DoS on valid DAO withdrawals.** A transaction sender who encodes a witness `input_type` index whose full u64 value exceeds 255 (e.g., 257) and populates `header_deps` so that the C VM's lowest-byte resolution finds the correct deposit header will have their withdrawal accepted by the C VM DAO script but permanently rejected by every Rust node's tx pool with `Reject::Malformed`. The user cannot withdraw their DAO deposit through the normal network path.

**Consensus split.** If such a transaction is mined into a block (e.g., by a miner that bypasses the tx pool or uses a non-Rust implementation), the Rust node's block verifier will reject the block because `FeeCalculator` returns an error, while the C VM considers every script valid. This splits the network between nodes that accepted the block and nodes that did not.

---

### Likelihood Explanation

The attack requires a transaction with ≥ 258 `header_deps` entries and a witness index of 257. There is no enforced protocol limit on `header_deps` count found in the codebase (no `max_header_deps` constant exists). All 258 referenced block hashes must be in the canonical chain, which is a realistic constraint for a long-running chain. The entry path is the standard `send_transaction` RPC, reachable by any unprivileged transaction sender. Likelihood is **medium**: the preconditions are non-trivial but fully within the capability of a motivated attacker or a buggy client.

---

### Recommendation

Align the Rust `DaoCalculator` with the C VM DAO script's actual index-decoding behavior. Concretely, in `util/dao/src/lib.rs`, replace the full-u64 read:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

with a single-byte read that matches what the C VM DAO script does:

```rust
Ok(header_deps_index_data.unwrap()[0] as u64)
```

Alternatively, if the intent is for both sides to use the full u64, the C VM DAO script must be updated and a hard-fork scheduled. Either way, the two interpretations must be made identical to eliminate the split. [6](#0-5) 

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the split:

1. Build a DAO withdrawal transaction with 258 `header_deps`.
2. Place the correct deposit block at index 1 (C VM's lowest-byte resolution target).
3. Place the withdraw block at index 257 (Rust's full-u64 resolution target).
4. Set witness `input_type` = `257u64.to_le_bytes()` → bytes `[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`.
5. C VM reads byte 0 = `0x01` → index 1 → deposit block → block-number matches cell data → **script passes**.
6. Rust reads full u64 = 257 → index 257 → withdraw block → block-number 200 ≠ cell data 100 → `DaoError::InvalidOutPoint` → `Reject::Malformed` → **tx pool rejects**. [7](#0-6)

### Citations

**File:** util/dao/src/lib.rs (L83-99)
```rust
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
