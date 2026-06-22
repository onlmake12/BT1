### Title
DAO Withdrawal `header_dep_index` Not Validated Against C VM's u8 Interpretation — Consensus Discrepancy Between Rust Verifier and On-Chain Script (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` in `util/dao/src/lib.rs` reads the `header_dep_index` from the transaction witness as a full `u64` and uses it unchecked to index into `header_deps`. The on-chain `dao.c` script reads the same field as a `u8` (lowest byte only). When a transaction sender supplies `header_dep_index > 255`, the Rust verifier and the C VM resolve different header deps, creating a consensus discrepancy. This is a direct analog to the external report's class: an unchecked index parameter used without validation against the actual collection bounds or against the interpretation used by the authoritative verifier.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit header by reading `header_dep_index` from `WitnessArgs.input_type` as a raw `u64` via `LittleEndian::read_u64`, then uses it directly to index into `header_deps()`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)   // ← unchecked u64 cast to usize
        .and_then(|hash| header_deps.get(&hash))
        .ok_or(DaoError::InvalidOutPoint)
})
```

There is no validation that `header_dep_index` fits within the range the on-chain `dao.c` script would compute. The `dao.c` script reads this field as a `u8` (lowest byte). When `header_dep_index = 257` (little-endian bytes: `[0x01, 0x01, 0x00, ...]`):

- **C VM** reads lowest byte → resolves `header_deps[1]`
- **Rust verifier** reads full u64 → resolves `header_deps[257]`

The codebase itself documents this discrepancy in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

```rust
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

The tx-pool admission path (`check_tx_fee` in `tx-pool/src/util.rs`) calls `DaoCalculator::transaction_fee` **without** running C VM script verification first. This means a crafted transaction can pass the Rust fee check while being rejected by the on-chain C VM, or vice versa.

---

### Impact Explanation

Two exploitable directions exist:

**Direction 1 — Tx-pool pollution / miner trap:**
A transaction sender crafts a DAO withdrawal where `header_dep_index = 257`, `header_deps[257]` = correct deposit block hash (Rust resolves correctly → fee check passes), but `header_deps[1]` = wrong block hash (C VM resolves incorrectly → script fails). The Rust node admits this transaction to the tx-pool. If a miner includes it in a block, the block fails C VM script verification and is rejected by the network. The miner loses the block reward.

**Direction 2 — Valid transaction censorship:**
A transaction sender crafts a DAO withdrawal where `header_dep_index = 257`, `header_deps[1]` = correct deposit block hash (C VM accepts), but `header_deps[257]` = wrong block hash (Rust rejects with `DaoError::InvalidOutPoint`). The Rust node rejects a transaction that is valid per the on-chain protocol. The DAO depositor cannot withdraw their funds through this node.

In both cases, the root cause is the same: `header_dep_index` is an unchecked transaction-sender-controlled parameter used without validating it against the C VM's u8 interpretation.

---

### Likelihood Explanation

The attacker must:
1. Submit a DAO withdrawal transaction via the `send_transaction` RPC (standard, unprivileged operation).
2. Craft the witness `input_type` field with `header_dep_index > 255`.
3. Pad `header_deps` to at least 258 entries.

This requires no privileged access, no keys, and no majority hashpower. The construction is unusual but fully within the protocol's allowed transaction structure. The `send_transaction` RPC is the standard entry point for any transaction sender.

---

### Recommendation

In `transaction_maximum_withdraw`, after reading `header_dep_index`, validate that it does not exceed 255 (to match the C VM's u8 interpretation) and that it is strictly less than `header_deps().len()`:

```rust
.and_then(|header_dep_index| {
    if header_dep_index > 255 {
        return Err(DaoError::InvalidDaoFormat);
    }
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)
        .and_then(|hash| header_deps.get(&hash))
        .ok_or(DaoError::InvalidOutPoint)
})
```

This aligns the Rust verifier with the C VM's u8 interpretation and eliminates the consensus discrepancy.

---

### Proof of Concept

The codebase already contains a test that proves the discrepancy exists and is reachable:

**File:** `util/dao/src/tests.rs`, lines 475–537 [1](#0-0) 

The test constructs a transaction with `header_dep_index = 257` (lowest byte = 1), places the correct deposit block at `header_deps[1]` (C VM resolves here) and the withdraw block at `header_deps[257]` (Rust resolves here). The test comment explicitly states: *"Rust resolves index 257 → withdraw block (number 200), but cell data says deposited at block 100."*

The root cause in production code:

**File:** `util/dao/src/lib.rs`, lines 91–99 [2](#0-1) 

The unchecked `header_dep_index as usize` cast at line 96 is the vulnerable statement. No upper-bound check against 255 (C VM's u8 limit) or against `header_deps().len()` is performed before use.

The tx-pool admission path that runs this check without C VM script verification:

**File:** `tx-pool/src/util.rs`, lines 28–53 [3](#0-2)

### Citations

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
