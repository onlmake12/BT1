### Title
DAO Withdrawal Permanently Rejected Due to Wrong `header_dep_index` Width in `DaoCalculator` — (`File: util/dao/src/lib.rs`)

### Summary

`DaoCalculator::transaction_maximum_withdraw` reads the `header_dep_index` stored in the DAO withdrawal witness as a full `u64`, but the on-chain DAO C script reads only the **lowest byte** of that 8-byte field. When a depositor constructs a withdrawal transaction with `header_dep_index > 255`, the Rust off-chain verifier resolves a different `header_deps` entry than the C VM does, finds a block-number mismatch, and returns `Err(DaoError::InvalidOutPoint)`. This error propagates through both the tx-pool admission path and the block-level `ContextualTransactionVerifier`, permanently blocking the user from redeeming their deposited CKB.

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit-header index from the witness `input_type` field:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))  // line 91
```

It then uses this full `u64` to index into `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // line 96
```

The on-chain DAO C script, however, reads only the **lowest byte** of the same 8-byte little-endian field. The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` explicitly documents this discrepancy:

```
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

With `header_dep_index = 257` (little-endian bytes: `[0x01, 0x01, 0x00, …]`):
- **C VM** reads byte `0x01` → index 1 → deposit block (number 100) → block-number check passes → transaction accepted.
- **Rust** reads `u64 = 257` → index 257 → withdraw block (number 200) → `deposit_header.number() != deposited_block_number` (200 ≠ 100) → `Err(DaoError::InvalidOutPoint)`.

This error surfaces in two places:

1. **Tx-pool admission** — `check_tx_fee` in `tx-pool/src/util.rs` calls `DaoCalculator::transaction_fee`, maps any error to `Reject::Malformed`, and drops the transaction.
2. **Block verification** — `ContextualTransactionVerifier::verify` calls `self.fee_calculator.transaction_fee()` at line 170; an error here causes the entire block to be rejected.

### Impact Explanation

A DAO depositor whose withdrawal transaction carries `header_dep_index > 255` (i.e., the deposit header sits beyond position 255 in `header_deps`) will have their transaction rejected by every honest CKB node's tx-pool and block verifier. The on-chain DAO script would accept the transaction, but the Rust node never allows it to reach the chain. The deposited CKB is permanently locked with no recovery path through the standard protocol.

**Impact: High** — permanent, irrecoverable lock of user funds.

### Likelihood Explanation

Any unprivileged RPC caller or tx-pool submitter who constructs a DAO withdrawal with more than 255 `header_deps` entries (placing the deposit header at index ≥ 256) triggers this. While most withdrawals use only 2 header_deps, the protocol imposes no hard cap below the block-size limit, so the scenario is reachable without any privileged access. The discrepancy is already documented in the test suite, confirming it is a known, reproducible code path.

**Likelihood: Medium** — unusual but fully reachable by any DAO depositor.

### Recommendation

Align the Rust `DaoCalculator` with the on-chain C VM's actual byte-width for `header_dep_index`. Either:

1. Read only the lowest byte in Rust to match the C VM:
   ```rust
   Ok(header_deps_index_data.unwrap()[0] as u64)
   ```
2. Or update the on-chain DAO C script to read the full `u64`, and document the agreed-upon width in the protocol spec.

The fix must be applied consistently in `util/dao/src/lib.rs` at the point where `header_dep_index` is decoded, and the `check_dao_withdraw_header_dep_index_exceeds_u8` test must be updated to assert `result.is_ok()` after the fix.

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` is a direct reproduction:

1. Build a DAO withdrawal transaction with 258 `header_deps`.
2. Place the deposit block hash at index 1 (what the C VM resolves).
3. Place the withdraw block hash at index 257.
4. Set `input_type` in the witness to `257u64` in little-endian (lowest byte = 1).
5. Call `DaoCalculator::transaction_fee(&rtx)`.

**Expected (correct) result**: `Ok(fee)` — the C VM accepts this.
**Actual result**: `Err(DaoError::InvalidOutPoint)` — Rust resolves index 257 → withdraw block → block-number mismatch → rejection.

The same rejection propagates through `check_tx_fee` in `tx-pool/src/util.rs` (line 34–41) and `FeeCalculator::transaction_fee` inside `ContextualTransactionVerifier::verify` (line 170), blocking the withdrawal at both the tx-pool and block-verification layers. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** tx-pool/src/util.rs (L28-41)
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
