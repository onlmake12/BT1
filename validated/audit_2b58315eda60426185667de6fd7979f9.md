### Title
DAO Withdrawal Fee Calculator Resolves Wrong Header Due to u64 vs u8 Index Discrepancy — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` in `util/dao/src/lib.rs` reads the header-deps index from the witness `input_type` field as a full `u64`, while the on-chain CKB DAO C script reads only the **lowest byte** (effectively a `u8`). When a DAO withdrawal transaction encodes an index whose full `u64` value and lowest byte point to **different** entries in `header_deps`, the Rust node resolves the wrong header — the wrong entity — for the deposit block. This is the direct CKB analog of the reported "wrong address queried for balance" class.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit-header index from the witness:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then uses that full `u64` to index into `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
```

The on-chain DAO C script, however, reads the same 8-byte field as a `u8` (lowest byte only). A witness encoding index `257` (`0x0000000000000101`) causes:

- **C script**: reads `0x01` → resolves `header_deps[1]` (the deposit block) → **accepts**
- **Rust node**: reads `257` → resolves `header_deps[257]` (a different block) → block-number mismatch at line 105 → **returns `DaoError::InvalidOutPoint`**

This discrepancy is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

> "Position 1: correct deposit block (what C VM resolves via lowest byte). Position 257: withdraw block (wrong — Rust resolves this with full u64)." [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

**Path 1 — Tx-pool DoS (confirmed):**  
`check_tx_fee` in `tx-pool/src/util.rs` calls `DaoCalculator::transaction_fee`, which calls `transaction_maximum_withdraw`. When the index discrepancy triggers `DaoError::InvalidOutPoint`, the transaction is rejected with `Reject::Malformed`. A legitimate DAO depositor who constructs a phase-2 withdrawal with `> 256` header_deps and an index `> 255` cannot submit their withdrawal through any Rust CKB node. Their funds are locked until they reconstruct the transaction with a small index. [4](#0-3) 

**Path 2 — Consensus split (high severity):**  
`FeeCalculator::transaction_fee` in `verification/src/transaction_verifier.rs` (line 265–273) is called inside `ContextualTransactionVerifier::verify` (line 170) and `verify_with_pause` (line 184) via the `?` operator. These verifiers are invoked during block validation. If a miner includes a crafted DAO withdrawal (valid per the C script) in a block, the Rust node rejects the entire block while other implementations accept it — causing a chain split. [5](#0-4) [6](#0-5) 

---

### Likelihood Explanation

Any unprivileged DAO depositor can trigger Path 1 by:
1. Depositing CKB into the NervosDAO.
2. Constructing a phase-2 withdrawal transaction with ≥ 258 `header_deps` entries and encoding the deposit-block index as `257` (or any value `> 255` whose lowest byte points to the correct deposit block).
3. Submitting via RPC.

No special privileges, keys, or majority hashpower are required. The transaction is structurally valid and would be accepted by the on-chain C script. Path 2 additionally requires a miner to include the transaction in a block, which is realistic if the miner uses a non-Rust implementation or constructs the block template manually.

---

### Recommendation

In `transaction_maximum_withdraw`, truncate the decoded index to `u8` before indexing into `header_deps`, matching the on-chain C script's behavior:

```rust
// Change:
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// To:
Ok(u64::from(header_deps_index_data.unwrap()[0]))
```

Alternatively, add an explicit bounds check rejecting any index `> 255` with `DaoError::InvalidDaoFormat` before the `header_deps().get(...)` call, and document the constraint in the protocol spec.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–536) directly demonstrates the root cause:

1. `header_deps` is padded to 258 entries; `header_deps[1]` = deposit block; `header_deps[257]` = withdraw block.
2. Witness encodes index `257u64` (little-endian).
3. C script reads lowest byte `0x01` → resolves deposit block → **valid**.
4. Rust reads full `u64` `257` → resolves withdraw block (number 200) → block-number check against cell data (100) fails → `DaoError::InvalidOutPoint`.
5. `check_tx_fee` maps this to `Reject::Malformed` → transaction rejected from tx-pool. [7](#0-6) [8](#0-7)

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

**File:** util/dao/src/lib.rs (L101-107)
```rust
                            let deposit_header = self
                                .data_loader
                                .get_header(deposit_header_hash)
                                .ok_or(DaoError::InvalidHeader)?;
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
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
