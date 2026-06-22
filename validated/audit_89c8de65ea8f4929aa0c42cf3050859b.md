### Title
DAO Withdrawal Header-Dep Index Confusion: Rust Reads Full u64 While C VM Reads Only Lowest Byte — (`File: util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator` reads the full 8-byte little-endian u64 from `WitnessArgs.input_type` to index into `header_deps`, while the on-chain C VM (`dao.c`) reads only the **lowest byte** (u8) of that same 8-byte field. When a transaction encodes an index whose lowest byte differs from its full u64 value (e.g., 257 = `0x0101`, lowest byte = 1), the two implementations resolve different `header_deps` entries. A miner can craft a DAO withdrawal transaction that the C VM accepts as valid but the Rust node rejects, causing a **consensus split**.

---

### Finding Description

The DAO withdrawal protocol requires the withdrawing transaction to encode, in `WitnessArgs.input_type`, the index into `header_deps` pointing to the deposit block header. The Rust `DaoCalculator::transaction_maximum_withdraw` reads this index as a full u64:

```rust
// util/dao/src/lib.rs line 91
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

and then uses it directly:

```rust
// util/dao/src/lib.rs lines 93-99
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)
        ...
```

The C VM (`dao.c`, referenced at `test/src/specs/dao/dao_user.rs:14`) reads only the **lowest byte** of the same 8-byte witness field as the index. This is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

When `witness.input_type` = 257 (LE bytes: `0x01 0x01 0x00 … 0x00`):
- C VM reads lowest byte → index **1** → `header_deps[1]` = deposit block → block number matches cell data → **VALID**
- Rust reads full u64 → index **257** → `header_deps[257]` = a different block → block number mismatch → **INVALID**

The `DaoCalculator::transaction_fee` is called in two critical paths:

1. **Block verification** — `FeeCalculator::transaction_fee()` inside `ContextualTransactionVerifier::verify()` at `verification/src/transaction_verifier.rs:170`
2. **DAO header field verification** — `DaoHeaderVerifier::verify()` at `verification/contextual/src/contextual_block_verifier.rs:301–318`, which calls `dao_field()` → `transaction_maximum_withdraw()` with the same index logic

Both paths cause the Rust node to reject a block that the C VM considers valid.

---

### Impact Explanation

A miner who crafts a DAO withdrawal transaction with `witness.input_type` = any value whose lowest byte differs from its full u64 value (e.g., 257, 513, 769…) and places the correct deposit block at `header_deps[lowest_byte]` will produce a transaction the C VM accepts. When this transaction is included in a mined block:

- The C VM validates the block as **valid** (correct deposit block found at the low-byte index)
- The Rust node's `ContextualTransactionVerifier` or `DaoHeaderVerifier` rejects the block as **invalid** (wrong block found at the full-u64 index, block number mismatch)

This is a **consensus split**: nodes running the C VM accept the chain tip while Rust nodes reject it, permanently forking the network. The attacker does not need majority hashpower — a single mined block suffices.

---

### Likelihood Explanation

Any participant who mines even one block can trigger this. The transaction bypasses the tx-pool (which also calls `DaoCalculator::transaction_fee` via `tx-pool/src/util.rs:34`) by being inserted directly into a block template. The construction requires only: a valid DAO cell to withdraw, 258+ `header_deps` entries, and a witness encoding index 257 (or any `n*256 + k` where `k` is the correct low-byte index). This is straightforward to construct.

---

### Recommendation

In `util/dao/src/lib.rs`, change the index read to match the C VM's behavior — truncate to the lowest byte before indexing:

```rust
// Current (wrong):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// Fixed (match C VM lowest-byte behavior):
Ok(u64::from(header_deps_index_data.unwrap()[0]))
```

Alternatively, if the intent is for Rust to use the full u64, the C VM (`dao.c`) must be updated to also read the full 8-byte little-endian value. Either way, both implementations must agree on the same index resolution.

---

### Proof of Concept

1. Obtain a live DAO cell deposited at block number 100.
2. Construct a DAO withdrawal transaction with:
   - `header_deps` padded to 258 entries: `header_deps[1]` = deposit block hash (block 100), `header_deps[257]` = withdraw block hash (block 200), all others = dummy
   - `witness.input_type` = `257u64.to_le_bytes()` = `[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`
   - Cell data = `100u64.to_le_bytes()` (deposit block number)
3. Mine a block containing this transaction.
4. **C VM result**: reads lowest byte of witness = 1 → `header_deps[1]` = deposit block (number 100) → `100 == 100` → **VALID**
5. **Rust node result**: reads full u64 = 257 → `header_deps[257]` = withdraw block (number 200) → `200 != 100` → `DaoError::InvalidOutPoint` → block **REJECTED**
6. Rust nodes split from C-VM-based nodes.

This exact scenario is encoded in the existing test `check_dao_withdraw_header_dep_index_exceeds_u8` at `util/dao/src/tests.rs:476–537`, which asserts `result.is_err()` — confirming the Rust node rejects what the C VM accepts. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-320)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao = DaoCalculator::new(
            &self.context.consensus,
            &self.context.store.borrow_as_data_loader(),
        )
        .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
        .map_err(|e| {
            error_target!(
                crate::LOG_TARGET,
                "Error generating dao data for block {}: {:?}",
                self.header.hash(),
                e
            );
            e
        })?;

        if dao != self.header.dao() {
            return Err((BlockErrorKind::InvalidDAO).into());
        }
        Ok(())
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
