### Title
DAO Withdrawal Rejected by Rust Node Due to `header_dep_index` Truncation Discrepancy vs. C VM — (`File: util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator` reads the `input_type` witness field as a full `u64` and uses it directly as an array index into `header_deps`. The on-chain `dao.c` script (running inside CKB-VM) reads the same 8-byte little-endian value but only uses the **lowest byte** (effectively `index & 0xFF`) to index into `header_deps`. When a DAO withdrawal transaction encodes `input_type = 257` (0x0101 LE), the C VM resolves `header_deps[1]` while the Rust node resolves `header_deps[257]`. This causes the Rust node to reject a transaction that the C VM would accept, producing a DoS against DAO withdrawals and a potential consensus split if such a transaction is mined into a block.

---

### Finding Description

**Root cause — `util/dao/src/lib.rs` lines 91–99:**

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)   // ← full u64 cast to usize
        .and_then(|hash| header_deps.get(&hash))
        .ok_or(DaoError::InvalidOutPoint)
})?;
```

The `header_dep_index` is a raw `u64` read from the witness and cast directly to `usize`. The `dao.c` script, however, reads the same 8-byte field but only uses the lowest byte as the index (equivalent to `index & 0xFF`).

**Documented discrepancy — `util/dao/src/tests.rs` lines 475–537:**

The test `check_dao_withdraw_header_dep_index_exceeds_u8` explicitly constructs a transaction with:
- 258 `header_deps` entries
- `input_type = 257` (LE bytes: `0x01, 0x01, 0x00, …`)
- `header_deps[1]` = deposit block (what C VM resolves: `257 & 0xFF = 1`)
- `header_deps[257]` = withdraw block (what Rust resolves: `257`)

The test asserts `result.is_err()` — confirming the Rust node rejects this transaction — while the C VM would accept it (using `header_deps[1]` = deposit block).

**Verification pipeline impact:**

`DaoCalculator::transaction_fee()` is called inside `ContextualTransactionVerifier::verify()` (line 170 in `verification/src/transaction_verifier.rs`):

```rust
let fee = self.fee_calculator.transaction_fee()?;
```

This is used in **both** tx-pool admission (`check_tx_fee` in `tx-pool/src/util.rs` line 34) and block validation. If `DaoCalculator` returns `DaoError::InvalidOutPoint` or a block-number mismatch error, the entire verification fails.

Separately, `CapacityVerifier::verify()` (lines 483–493 in `verification/src/transaction_verifier.rs`) **skips** the capacity overflow check for DAO withdrawals:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    // capacity check — skipped for DAO withdrawals
}
```

This means the Rust node relies on the C VM script for DAO capacity enforcement, but the `FeeCalculator` (using `DaoCalculator`) still runs and can fail independently of the C VM result.

---

### Impact Explanation

**Scenario A — Tx-pool DoS:**
A DAO depositor crafts a withdrawal with `input_type = 257`, placing the deposit block at `header_deps[1]`. The C VM (dao.c) accepts the transaction. The Rust `DaoCalculator` resolves `header_deps[257]` (a different block), finds a block-number mismatch with the cell data, and returns `DaoError::InvalidOutPoint`. `check_tx_fee` propagates this as a `Reject`, and the transaction is permanently excluded from the tx-pool. The depositor cannot withdraw their DAO funds through the normal path.

**Scenario B — Consensus split (if mined directly):**
If a miner assembles a block containing such a transaction (bypassing the tx-pool), `ContextualTransactionVerifier::verify()` calls `fee_calculator.transaction_fee()`, which calls `DaoCalculator::transaction_fee()`. This fails, causing the Rust node to reject the block as invalid — even though the C VM script accepted the transaction. All honest Rust nodes would reject the block, creating a fork if any non-Rust-node implementation accepted it.

---

### Likelihood Explanation

- Requires `input_type > 255`, meaning the transaction must have ≥ 256 `header_deps` entries. Normal DAO withdrawals use 2 (deposit block + withdraw block), so this is unusual but not impossible.
- A script author or tx-pool submitter can craft this deliberately.
- No privileged access, key material, or majority hashpower is required.
- The discrepancy is already documented in the test suite, confirming the developers are aware of the behavioral difference but have not patched the Rust side.

---

### Recommendation

In `DaoCalculator::transaction_maximum_withdraw()`, truncate the `header_dep_index` to its lowest byte before indexing, matching the C VM behavior:

```rust
// Before:
.get(header_dep_index as usize)

// After (matching dao.c lowest-byte semantics):
.get((header_dep_index & 0xFF) as usize)
```

Alternatively, add a validation step that rejects any `input_type` value whose upper bytes are non-zero, making the Rust node and C VM agree on what constitutes a valid index.

---

### Proof of Concept

The existing test in `util/dao/src/tests.rs` already demonstrates the discrepancy: [1](#0-0) 

The critical indexing line in the Rust node: [2](#0-1) 

The `CapacityVerifier` skip that makes the Rust node rely on the C VM for DAO capacity enforcement (while `FeeCalculator` still runs independently): [3](#0-2) 

The `FeeCalculator` call inside `ContextualTransactionVerifier::verify()` that propagates the `DaoCalculator` failure as a hard verification error: [4](#0-3)

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

**File:** verification/src/transaction_verifier.rs (L162-172)
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
    }
```

**File:** verification/src/transaction_verifier.rs (L479-494)
```rust
        // skip OutputsSumOverflow verification for resolved cellbase and DAO
        // withdraw transactions.
        // cellbase's outputs are verified by RewardVerifier
        // DAO withdraw transaction is verified via the type script of DAO cells
        if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
            let inputs_sum = self.resolved_transaction.inputs_capacity()?;
            let outputs_sum = self.resolved_transaction.outputs_capacity()?;

            if inputs_sum < outputs_sum {
                return Err((TransactionError::OutputsSumOverflow {
                    inputs_sum,
                    outputs_sum,
                })
                .into());
            }
        }
```
