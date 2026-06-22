### Title
Missing Bounds Validation on DAO Withdrawal `header_dep_index` Enables Consensus Split — (File: `util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator` reads the `header_dep_index` from the DAO withdrawal witness as a full `u64` without validating it is within the valid range of `header_deps` indices. The on-chain C DAO script (`dao.c`) interprets this same field using only the lowest byte (treating it as a `u8`), so when `header_dep_index > 255`, the Rust node and the C VM resolve different `header_deps` entries. This discrepancy is explicitly documented in the test suite and constitutes a consensus split: a transaction sender can craft a DAO withdrawal that the C VM accepts but the Rust node rejects, causing Rust nodes to reject valid blocks.

---

### Finding Description

In `util/dao/src/lib.rs`, the function `transaction_maximum_withdraw` reads the `header_dep_index` from the witness `input_type` field as a full `u64` (line 91) and uses it directly to index into `header_deps()` (line 96):

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
``` [1](#0-0) 

There is no check that `header_dep_index < header_deps().len()` before the access, and critically, no check that `header_dep_index <= u8::MAX`. The on-chain C DAO script (`dao.c`) reads the same 8-byte `input_type` field but uses only the lowest byte as the index (i.e., `index = witness_value & 0xFF`). When `header_dep_index > 255`, the two implementations resolve different entries in `header_deps`.

This discrepancy is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

- `header_deps[1]` = deposit block (block 100) — what the C VM resolves via `257 & 0xFF = 1`
- `header_deps[257]` = withdraw block (block 200) — what the Rust code resolves via full `u64 = 257`
- Witness `input_type` = `257u64` (little-endian)
- Cell data = `100` (deposited block number)

The Rust code resolves index 257 → withdraw block (number 200) → block number mismatch with cell data (100) → `DaoError::InvalidOutPoint`. The C VM resolves index 1 → deposit block (number 100) → block number matches → **accepts the transaction**. [2](#0-1) 

The `DaoCalculator` is used in the consensus-critical verification pipeline: [3](#0-2) [4](#0-3) 

---

### Impact Explanation

A transaction sender crafts a DAO withdrawal transaction with:
- 258+ `header_deps` entries
- `input_type` witness = `257u64` (LE), so lowest byte = `1`
- `header_deps[1]` = the correct deposit block hash
- `header_deps[257]` = any other block hash

The C VM on-chain script accepts this transaction (index 1 → correct deposit block → block number matches). The Rust `DaoCalculator` rejects it (index 257 → wrong block → block number mismatch → `DaoError::InvalidOutPoint`). If a miner (running a non-Rust implementation or a patched node) includes this transaction in a block, all standard Rust CKB nodes reject the block. This is a **consensus split / chain split** triggered by an unprivileged transaction sender.

---

### Likelihood Explanation

Medium. The attacker must:
1. Construct a valid DAO deposit and withdrawal transaction pair
2. Pad `header_deps` to at least 258 entries (no consensus rule caps `header_deps` count at 255)
3. Arrange for a miner to include the crafted withdrawal transaction

Steps 1–2 are fully within reach of an unprivileged transaction sender. Step 3 requires a cooperating or non-Rust miner, which is realistic in a heterogeneous network.

---

### Recommendation

Add an explicit upper-bound check on `header_dep_index` before using it to index into `header_deps`. Specifically, validate that `header_dep_index` fits within the range the on-chain C DAO script uses (i.e., `header_dep_index <= u8::MAX`) and that it is less than `header_deps().len()`:

```rust
if header_dep_index > u8::MAX as u64
    || header_dep_index as usize >= rtx.transaction.header_deps().len()
{
    return Err(DaoError::InvalidDaoFormat);
}
```

This aligns the Rust verification logic with the on-chain C script's index interpretation and eliminates the consensus split. [5](#0-4) 

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the discrepancy:

```
header_deps[1]   = deposit_block  (block 100)   ← C VM resolves (257 & 0xFF = 1)
header_deps[257] = withdraw_block (block 200)   ← Rust resolves (full u64 = 257)
witness input_type = 257u64 (LE)
cell data = 100 (deposited block number)

C VM:   index 1   → block 100 → matches cell data → ACCEPT
Rust:   index 257 → block 200 → mismatch (200 ≠ 100) → REJECT (DaoError::InvalidOutPoint)
```

The test asserts `result.is_err()`, confirming the Rust node rejects a transaction the C VM would accept. [6](#0-5)

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

**File:** verification/src/transaction_verifier.rs (L1-10)
```rust
use crate::cache::Completed;
use crate::error::TransactionErrorSource;
use crate::{TransactionError, TxVerifyEnv};
use ckb_chain_spec::consensus::Consensus;
use ckb_constant::consensus::ENABLED_SCRIPT_HASH_TYPE;
use ckb_dao::DaoCalculator;
use ckb_dao_utils::DaoError;
use ckb_error::Error;
#[cfg(not(target_family = "wasm"))]
use ckb_script::ChunkCommand;
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L1-10)
```rust
use crate::uncles_verifier::{UncleProvider, UnclesVerifier};
use ckb_async_runtime::Handle;
use ckb_chain_spec::{
    consensus::{Consensus, ConsensusProvider},
    versionbits::VersionbitsIndexer,
};
use ckb_dao::DaoCalculator;
use ckb_dao_utils::DaoError;
use ckb_error::{Error, InternalErrorKind};
use ckb_logger::error_target;
```
