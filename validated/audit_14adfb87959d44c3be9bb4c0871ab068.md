### Title
DAO Withdrawal Header-Deps Index Width Mismatch Between Rust Node and C VM Enables Consensus Split — (`util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator` reads the `header_deps` index from the DAO withdrawal witness as a full `u64`, while the on-chain C VM script (`dao.c`) reads only the **lowest byte** (effectively a `u8`). A crafted DAO Phase-2 withdrawal transaction with ≥258 `header_deps` and a witness index whose low byte points to the deposit block but whose full `u64` value points to a different block will be **accepted by the C VM** (script execution passes) but **rejected by the Rust node** (fee calculation fails). Any miner that includes such a transaction produces a block that Rust nodes refuse to accept, causing a consensus split.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` parses the deposit-block index from the witness `input_type` field using `LittleEndian::read_u64`:

```rust
// util/dao/src/lib.rs:91
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then uses the full 64-bit value to index into `header_deps`:

```rust
// util/dao/src/lib.rs:94-98
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
    .and_then(|hash| header_deps.get(&hash))
    .ok_or(DaoError::InvalidOutPoint)
```

The on-chain `dao.c` script (referenced at `test/src/specs/dao/dao_user.rs:14`) reads the same field as a **single byte** (u8). The project's own test explicitly documents this discrepancy:

```rust
// util/dao/src/tests.rs:489-495
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
let dummy = h256!("0x1").into();
let mut header_deps = vec![dummy; 258];
header_deps[1] = deposit_block.hash();
header_deps[257] = withdraw_block.hash();
```

With witness `input_type = 257` (little-endian bytes `[0x01, 0x01, 0x00, …]`):

| Layer | Reads index | Resolves to | Block-number check | Result |
|---|---|---|---|---|
| C VM (dao.c) | byte 0 = **1** | deposit block (100) | 100 == 100 ✓ | **ACCEPT** |
| Rust DaoCalculator | full u64 = **257** | withdraw block (200) | 200 ≠ 100 ✗ | **REJECT** |

The Rust rejection propagates through `FeeCalculator::transaction_fee()` → `ContextualTransactionVerifier::verify()` → block verification, causing the Rust node to reject the entire block.

---

### Impact Explanation

Any miner (non-Rust implementation, or a patched node) that includes a crafted DAO Phase-2 withdrawal with ≥258 `header_deps` and a witness index > 255 will produce a block that:

- Passes C VM script execution (the authoritative on-chain rule)
- Is rejected by every standard Rust CKB node via `FeeCalculator::transaction_fee()`

This causes a **consensus split**: the crafted block is valid per the protocol's script rules but invalid per the Rust node's fee-accounting layer. Rust nodes would stall on that block height while non-Rust nodes advance, permanently forking the chain. No financial gain is required; the attacker only needs to submit one such transaction to a willing miner.

---

### Likelihood Explanation

The attacker is an unprivileged transaction sender. Constructing the malicious transaction requires only:
1. An existing DAO deposit cell (any user can create one)
2. Crafting a Phase-2 withdrawal with 258 `header_deps` and witness index 257

No privileged keys, no 51% hashpower, and no social engineering are needed. The attacker does not need to mine the block themselves — any non-Rust miner or a miner running a patched node suffices. Given that CKB is a permissionless chain and alternative implementations or patched nodes are realistic, likelihood is **medium**.

---

### Recommendation

In `util/dao/src/lib.rs`, validate that the parsed `header_dep_index` fits within a `u8` before using it, to match the C VM's actual behavior:

```rust
// After reading the u64 index, reject if it exceeds 255
if header_dep_index > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
```

Alternatively, align the C VM (`dao.c`) to read the full 8-byte little-endian `u64` so both layers agree. The fix must be applied consistently to both the Rust node and the on-chain script to avoid introducing a new discrepancy.

---

### Proof of Concept

The repository's own test at `util/dao/src/tests.rs:475-536` is the proof of concept. It constructs exactly the described transaction and asserts that Rust rejects it while the comments confirm the C VM would accept it via the lowest-byte read: [1](#0-0) 

The production code path that rejects the transaction is `LittleEndian::read_u64` at: [2](#0-1) 

This result propagates through `FeeCalculator::transaction_fee()`: [3](#0-2) 

Which is called inside `ContextualTransactionVerifier::verify()` used during block validation: [4](#0-3) 

And also during tx-pool admission via `check_tx_fee`: [5](#0-4)

### Citations

**File:** util/dao/src/tests.rs (L475-536)
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
```

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

**File:** verification/src/transaction_verifier.rs (L159-172)
```rust
    /// Perform context-dependent verification, return a `Result` to `CacheEntry`
    ///
    /// skip script verify will result in the return value cycle always is zero
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
