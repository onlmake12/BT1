### Title
Rust `DaoCalculator` Reads Witness Header-Dep Index as Full `u64` While C DAO Script Reads Only the Lowest Byte, Causing Divergent Withdrawal Accounting and Tx-Pool Pollution — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` in `util/dao/src/lib.rs` decodes the `WitnessArgs.input_type` field as a full little-endian `u64` to look up the deposit block header in `header_deps`. The on-chain C DAO script (`dao.c`) reads the same field but interprets only the lowest byte as the index. When a witness encodes an index whose value exceeds 255 (e.g., 257 = `0x0101`), the two implementations resolve different entries in `header_deps`. The Rust node's block-number cross-check (`deposit_header.number() != deposited_block_number`) can pass for the Rust-resolved header while the C script's equivalent check fails for the C-resolved header. The result is that the Rust tx-pool accepts a DAO withdrawal transaction that the C DAO script will reject on-chain, constituting tx-pool pollution with a realistic, unprivileged entry path.

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
```

The C DAO script (referenced in the codebase at `test/src/specs/dao/dao_user.rs:14`) reads the same 8-byte field but treats only the lowest byte as the array index. For any witness value whose full `u64` and lowest byte differ (i.e., value > 255), the two implementations index into different positions of `header_deps`.

**Exploitable scenario (reverse of the test case):**

Construct a DAO phase-2 withdrawal transaction where:
- `header_deps[1]` = the **prepare-phase block** (block number 200)
- `header_deps[257]` = the **deposit block** (block number 100)
- Cell data stores `deposited_block_number = 100`
- Witness `input_type` = `257u64` (little-endian 8 bytes)

Rust resolves index 257 → deposit block (number 100). The block-number check `deposit_header.number() (100) == deposited_block_number (100)` passes. Rust computes `maximum_withdraw` using the correct deposit header and accepts the transaction into the tx-pool.

The C DAO script resolves index `257 & 0xFF = 1` → prepare-phase block (number 200). Its block-number check `200 ≠ 100` fails, and the script aborts. Any block containing this transaction is invalid.

The `check_tx_fee` path in `tx-pool/src/util.rs` calls `DaoCalculator::transaction_fee`, which calls `transaction_maximum_withdraw`, so the tx-pool admission decision is made entirely on the Rust-computed (incorrect) base.

---

### Impact Explanation

1. **Tx-pool pollution**: A DAO depositor submits crafted withdrawal transactions that the Rust node accepts (fee check passes, block-number check passes) but that the C DAO script rejects on-chain. These transactions permanently occupy tx-pool slots and are never committed.
2. **Incorrect `calculate_dao_maximum_withdraw` RPC output**: The RPC in `rpc/src/module/experiment.rs` calls `DaoCalculator::calculate_maximum_withdraw` directly with the caller-supplied header hashes, so it can also return a value inconsistent with what the C script enforces.
3. **Miner resource waste**: A miner that includes such a transaction produces an invalid block, wasting proof-of-work.

---

### Likelihood Explanation

Any CKB holder who has deposited into the NervosDAO can exploit this. The attacker:
1. Deposits CKB into the DAO (phase 1) — no privilege required.
2. Submits a phase-2 prepare transaction — no privilege required.
3. Crafts a phase-3 withdrawal transaction with a witness index of 257 and a `header_deps` array padded to 258 entries, placing the deposit block at position 257 and any other block at position 1.
4. Submits via the standard `send_transaction` RPC.

No trusted role, no majority hashpower, no social engineering is required. The only prerequisite is owning a live DAO cell.

---

### Recommendation

In `transaction_maximum_withdraw`, after decoding the full `u64` index, validate that it fits in a `u8` (i.e., `header_dep_index <= 255`) and return `DaoError::InvalidDaoFormat` otherwise. This aligns the Rust admission check with the C DAO script's actual index width, preventing the divergence:

```rust
let index = LittleEndian::read_u64(&header_deps_index_data.unwrap());
if index > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
```

Alternatively, if the C DAO script is updated to read a full `u64`, the Rust side requires no change, but the C script must be updated and deployed via a hard fork.

---

### Proof of Concept

The discrepancy is directly documented in the production test suite. The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` constructs exactly this scenario:

- `header_deps[1]` = deposit block (number 100)
- `header_deps[257]` = withdraw block (number 200)
- Witness index = 257

The test comment reads:
> "Position 1: correct deposit block (what C VM resolves via lowest byte). Position 257: withdraw block (wrong — Rust resolves this with full u64)."

The test asserts `result.is_err()` for this direction (Rust resolves to the wrong block, block-number check fails). The **reverse** direction — `header_deps[1]` = wrong block, `header_deps[257]` = correct deposit block — is not tested and causes Rust to accept while C rejects.

**Root cause line:** [1](#0-0) 

**Tx-pool admission call site:** [2](#0-1) 

**Fee computation entry point:** [3](#0-2) 

**Test documenting the discrepancy:** [4](#0-3) 

**C DAO script reference in production code:** [5](#0-4)

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

**File:** util/dao/src/lib.rs (L91-96)
```rust
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
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

**File:** test/src/specs/dao/dao_user.rs (L14-15)
```rust
// https://github.com/nervosnetwork/ckb-system-scripts/blob/1fd4cd3e2ab7e5ffbafce1f60119b95937b3c6eb/c/dao.c#L81
pub const LOCK_PERIOD_EPOCHS: u64 = 180;
```
