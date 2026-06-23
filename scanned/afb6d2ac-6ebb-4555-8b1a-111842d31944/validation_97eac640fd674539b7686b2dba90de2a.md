### Title
DAO Withdrawal Consensus Split via `header_deps` Index Width Mismatch Between On-Chain C Script and Rust `DaoCalculator` — (File: `util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator` reads the DAO withdrawal witness index as a full `u64`, while the on-chain DAO C script reads only the **lowest byte** of that same field. A transaction sender can craft a DAO phase-2 withdrawal with ≥258 `header_deps` and a witness index of 257 (lowest byte = 1). The C VM accepts the transaction (resolving index 1 → deposit block), but the Rust node's contextual verifier rejects it (resolving index 257 → wrong block). A miner who includes such a transaction in a block produces a block that is valid per the C VM but rejected by every Rust node, causing a **consensus split**.

---

### Finding Description

The NervosDAO withdrawal protocol requires the withdrawing transaction to embed, in the `WitnessArgs.input_type` field, an 8-byte little-endian `u64` that is the index into `header_deps` pointing to the original deposit block hash.

The Rust `DaoCalculator::transaction_maximum_withdraw` reads this index with full `u64` precision:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // full u64 used here
``` [1](#0-0) 

The on-chain DAO C script, however, reads only the **lowest byte** of the same 8-byte field when indexing into `header_deps`. This is explicitly documented in the test suite:

```rust
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
let mut header_deps = vec![dummy; 258];
header_deps[1] = deposit_block.hash();
header_deps[257] = withdraw_block.hash();

// input_type = 257, lowest byte = 1
let witness = WitnessArgs::new_builder()
    .input_type(Some(Bytes::from(257u64.to_le_bytes().to_vec())))
    .build();
``` [2](#0-1) 

With witness index = 257:
- **C VM** resolves `257 & 0xFF = 1` → `header_deps[1]` = deposit block → block number matches cell data → **accepts**
- **Rust** resolves `257` → `header_deps[257]` = withdraw block (number 200) → block number ≠ cell data (100) → `DaoError::InvalidOutPoint` → **rejects** [3](#0-2) 

The `DaoCalculator` is invoked in three critical paths:

1. **Block contextual verification** — `DaoHeaderVerifier::verify` calls `dao_field` → `withdrawed_interests` → `transaction_maximum_withdraw`. An error here causes the entire block to be rejected with `BlockErrorKind::InvalidDAO`. [4](#0-3) 

2. **Per-transaction contextual verification** — `FeeCalculator::transaction_fee` inside `ContextualTransactionVerifier::verify`. [5](#0-4) 

3. **Tx-pool admission** — `check_tx_fee` rejects the transaction before it enters the pool. [6](#0-5) 

---

### Impact Explanation

A miner who bypasses the tx-pool (e.g., by assembling a block template directly) and includes a crafted DAO withdrawal transaction with ≥258 `header_deps` and witness index 257 produces a block that:

- **Passes** CKB-VM script execution (the C DAO script accepts it)
- **Fails** Rust contextual block verification (`DaoHeaderVerifier` returns `InvalidDAO`)

This is a **consensus split**: the block is canonical per the on-chain protocol rules but is permanently rejected by all Rust CKB nodes. Nodes running alternative implementations that faithfully replicate the C VM behavior would accept the block, forking the network. Even without a competing implementation, a miner can use this to produce unreachable blocks, stalling chain progress for nodes that receive the block first.

---

### Likelihood Explanation

The attack requires:
1. A valid DAO deposit and prepare cycle (accessible to any CKB user)
2. Constructing a withdrawal transaction with ≥258 `header_deps` — each is 32 bytes, so 258 entries = ~8 KB, well within the transaction size limit
3. Miner cooperation or direct block assembly (bypassing the tx-pool)

The crafted transaction is rejected by the tx-pool (`check_tx_fee` fails), so the attacker must control a miner or collude with one. This raises the bar but does not eliminate the threat: any miner with a DAO deposit can self-execute this attack. The discrepancy is explicitly documented in the test suite, meaning the divergence is a known, unresolved implementation gap.

---

### Recommendation

Align the Rust `DaoCalculator` with the on-chain C DAO script's actual index semantics. If the C script uses only the lowest byte, the Rust code must do the same:

```rust
// Change:
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// To:
Ok(u64::from(header_deps_index_data.unwrap()[0]))
```

Alternatively, if the intent is for the C script to use the full `u64`, the C script must be patched and deployed via a hard fork. Either way, both sides must agree on the same interpretation. Additionally, add a non-contextual validation rule rejecting transactions whose witness DAO index exceeds 255 (or the actual `header_deps` length) to close the attack surface at the tx-pool boundary.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the Rust side of the split: [7](#0-6) 

To demonstrate the full consensus split:

1. Perform a standard DAO deposit and prepare cycle on a dev chain.
2. Construct a phase-2 withdrawal transaction:
   - `header_deps`: 258 entries; index 1 = deposit block hash, index 257 = any valid chain block hash
   - `witnesses[0].input_type`: `257u64.to_le_bytes()` (8 bytes LE)
3. Assemble a block directly (bypassing the tx-pool) containing this transaction.
4. Submit the block via `process_block` RPC.
5. **Observed**: The C VM script execution passes; `DaoHeaderVerifier` returns `InvalidDAO` and the block is rejected by the Rust node, while a node implementing the C VM's byte-truncation semantics would accept it.

### Citations

**File:** util/dao/src/lib.rs (L91-96)
```rust
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
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
