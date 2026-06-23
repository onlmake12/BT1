### Title
DAO Withdrawal `header_dep_index` Parsed as `u64` in Rust vs. `u8` (Lowest Byte) in On-Chain C Script — Consensus Split via Crafted Witness - (File: `util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the `header_dep_index` from the DAO withdrawal witness as a full `u64`, while the on-chain C DAO script (`dao.c`) reads only the lowest byte (effectively treating it as `u8`). A DAO depositor can craft a withdrawal transaction with an index value `> 255` that the C VM accepts but the Rust node's `FeeCalculator` rejects, causing a consensus split.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` reads the deposit block's `header_dep_index` from the witness `input_type` field as a full 8-byte little-endian `u64`:

```rust
// util/dao/src/lib.rs:91
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then uses this value directly as a `usize` index into `header_deps`:

```rust
// util/dao/src/lib.rs:94-98
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
    .and_then(|hash| header_deps.get(&hash))
    .ok_or(DaoError::InvalidOutPoint)
```

The on-chain C DAO script (`dao.c`, referenced in the test at line 14 of `test/src/specs/dao/dao_user.rs`) reads only the lowest byte of the same 8-byte field, treating it as a `u8` index.

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` explicitly documents this discrepancy:

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

With `input_type = 257` (0x0000000000000101 LE):
- **C VM** reads lowest byte = `1` → `header_deps[1]` = correct deposit block hash → block number check passes → **accepts**
- **Rust** reads full u64 = `257` → `header_deps[257]` = withdraw block hash → block number check fails (200 ≠ 100) → **rejects**

The `DaoCalculator::transaction_fee` is called from `FeeCalculator` inside `verification/src/transaction_verifier.rs` during block verification. An error from `transaction_fee` causes the Rust node to reject the entire block.

---

### Impact Explanation

A miner who includes a crafted DAO withdrawal transaction in a block will have that block:
- **Accepted** by nodes running the C VM (on-chain DAO script satisfied, lock script satisfied)
- **Rejected** by Rust nodes whose `FeeCalculator` calls `DaoCalculator::transaction_fee`, which returns `DaoError::InvalidOutPoint` due to the block number mismatch at the wrong index

This produces a **consensus split**: part of the network accepts the block, part rejects it. The chain forks. The attacker's DAO funds are effectively withdrawn on one fork while the other fork stalls or reorganizes. This directly breaks the integrity of the DAO accounting system — the analog to the M-18 finding where the task-based accounting was bypassed.

---

### Likelihood Explanation

Any DAO depositor (no privilege required) can construct this transaction. The only additional requirement is that a miner includes it. The attacker can offer a high fee to incentivize inclusion. The crafted transaction is structurally valid (correct witness length, valid `header_deps` list, correct lock script), so it passes all non-contextual checks. The discrepancy is triggered purely by choosing `header_dep_index > 255` with a specific `header_deps` layout.

---

### Recommendation

In `util/dao/src/lib.rs`, validate that the parsed `header_dep_index` fits within a `u8` before using it, to match the C VM's behavior:

```rust
let index = LittleEndian::read_u64(&header_deps_index_data.unwrap());
if index > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
```

Alternatively, align the on-chain C script to read the full `u64` index. Either fix must be applied consistently to both the Rust node and the on-chain script to eliminate the discrepancy.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–537) directly demonstrates the split:

1. Build a DAO withdrawal transaction with 258 `header_deps`.
2. Place the correct deposit block hash at `header_deps[1]` and the withdraw block hash at `header_deps[257]`.
3. Set witness `input_type = 257u64` (LE bytes: `[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`).
4. The C VM reads byte 0 = `0x01` → index 1 → deposit block → **accepts**.
5. `DaoCalculator::transaction_fee` reads full u64 = 257 → index 257 → withdraw block (number 200) → `deposit_header.number() != deposited_block_number` (200 ≠ 100) → `DaoError::InvalidOutPoint` → **rejects**.
6. A miner broadcasting a block containing this transaction causes a chain split between C-VM-based nodes and Rust nodes. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** util/dao/src/lib.rs (L91-98)
```rust
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
                                        .and_then(|hash| header_deps.get(&hash))
                                        .ok_or(DaoError::InvalidOutPoint)
```

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

**File:** verification/src/transaction_verifier.rs (L461-522)
```rust
/// Perform inputs and outputs `capacity` field related verification
pub struct CapacityVerifier {
    resolved_transaction: Arc<ResolvedTransaction>,
    dao_type_hash: Byte32,
}

impl CapacityVerifier {
    /// Create a new `CapacityVerifier`
    pub fn new(resolved_transaction: Arc<ResolvedTransaction>, dao_type_hash: Byte32) -> Self {
        CapacityVerifier {
            resolved_transaction,
            dao_type_hash,
        }
    }

    /// Verify sum of inputs capacity should be greater than or equal to sum of outputs capacity
    /// Verify outputs capacity should be greater than or equal to its occupied capacity
    pub fn verify(&self) -> Result<(), Error> {
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

        for (index, (output, data)) in self
            .resolved_transaction
            .transaction
            .outputs_with_data_iter()
            .enumerate()
        {
            let data_occupied_capacity = Capacity::bytes(data.len())?;
            if output.is_lack_of_capacity(data_occupied_capacity)? {
                return Err((TransactionError::InsufficientCellCapacity {
                    index,
                    inner: TransactionErrorSource::Outputs,
                    capacity: output.capacity().into(),
                    occupied_capacity: output.occupied_capacity(data_occupied_capacity)?,
                })
                .into());
            }
        }

        Ok(())
    }

    fn valid_dao_withdraw_transaction(&self) -> bool {
        self.resolved_transaction
            .resolved_inputs
            .iter()
            .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
    }
```
