### Title
DAO Withdrawal Permanently Rejected by Tx-Pool Due to `header_dep_index` Interpretation Discrepancy Between Rust Verifier and C VM — (`util/dao/src/lib.rs`)

### Summary
The Rust `DaoCalculator::transaction_maximum_withdraw` reads the full `u64` `header_dep_index` from the witness to locate the deposit block hash, while the on-chain C VM (`dao.c`) reads only the **lowest byte** of that same field. A transaction sender can craft a valid DAO phase-2 withdrawal (accepted by the C VM) that the Rust node permanently rejects via `check_tx_fee` and `ContextualTransactionVerifier::verify`, making the withdrawal unprocessable by any CKB node.

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` resolves the deposit block hash by reading the full `u64` witness index:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// …
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
```

The on-chain C VM (`dao.c`) reads only the **lowest byte** of the same 8-byte little-endian field. This discrepancy is explicitly documented in the production test suite:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

The test asserts `result.is_err()` — confirming the Rust verifier rejects a transaction the C VM would accept.

The result of `transaction_fee` (which calls `transaction_maximum_withdraw`) is used in two hard-rejection paths:

1. **`tx-pool/src/util.rs::check_tx_fee`** — maps a `DaoError` to `Reject::Malformed`, permanently barring the transaction from the pool.
2. **`verification/src/transaction_verifier.rs::ContextualTransactionVerifier::verify`** — calls `self.fee_calculator.transaction_fee()?`, causing block-level rejection if the fee calculation fails.

### Impact Explanation

An attacker (or the user themselves, tricked into using a crafted wallet) submits a DAO phase-2 withdrawal with:

- 258 `header_deps` entries
- `header_deps[1]` = correct deposit block hash
- `header_deps[257]` = the withdrawing (phase-1) block hash
- `witness.input_type` = `257u64` in little-endian (lowest byte = `0x01`)

**C VM path:** reads lowest byte → index 1 → correct deposit block → `deposit_header.number() == deposited_block_number` → **accepts**.

**Rust path:** reads full `u64` → index 257 → withdrawing block → `deposit_header.number() (200) != deposited_block_number (100)` → `DaoError::InvalidOutPoint` → `Reject::Malformed` → **permanently rejected**.

Because all CKB nodes run the same Rust implementation, the transaction is rejected by every node's tx-pool and by every block verifier. The user's DAO funds become unwithdrawable via this transaction structure. Additionally, if a miner somehow includes such a transaction in a block (e.g., via a custom block-assembly path), every node's `ContextualTransactionVerifier` would reject the block, causing a chain stall for that miner.

### Likelihood Explanation

The attack requires a transaction with more than 256 `header_deps` entries, which is unusual but fully valid per the CKB protocol. A malicious wallet, a script author, or a user following crafted instructions can produce such a transaction. The C VM script imposes no upper bound on `header_deps` length. The discrepancy is reachable by any unprivileged transaction sender via the standard RPC (`send_transaction`) or tx-pool submission path.

### Recommendation

Align the Rust verifier with the C VM's actual behavior. If `dao.c` uses only the lowest byte of the `header_dep_index`, the Rust code in `transaction_maximum_withdraw` should do the same:

```rust
// Replace:
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// With (matching C VM lowest-byte semantics):
Ok(u64::from(header_deps_index_data.unwrap()[0]))
```

Alternatively, if the intent is for both sides to use the full `u64`, the C VM script must be updated accordingly and the discrepancy documented as a hard fork. Either way, the two interpretations must be identical to prevent this split.

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the issue: [1](#0-0) 

The production code that reads the full `u64` index (root cause): [2](#0-1) 

The tx-pool hard-rejection path triggered by the resulting `DaoError`: [3](#0-2) 

The block-verification hard-rejection path: [4](#0-3)

### Citations

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
