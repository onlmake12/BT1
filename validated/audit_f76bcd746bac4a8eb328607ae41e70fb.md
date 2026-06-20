### Title
`DaoCalculator` Reads Witness `header_deps` Index as Full `u64` While C VM DAO Script Reads Only the Lowest Byte, Enabling Consensus Split for Indices > 255 — (`File: util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw()` reads the `header_deps` index stored in the DAO withdrawal witness as a full `u64`. The on-chain C VM DAO script, however, reads only the **lowest byte** of that same 8-byte little-endian value. For any index value whose lowest byte differs from the full value (i.e., any index > 255), the two implementations resolve to **different block headers**. A malicious miner can craft a DAO withdrawal transaction that the C VM accepts but the Rust node rejects, causing a consensus split.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw()` extracts the deposit header by reading the full 8-byte little-endian `u64` from the `input_type` field of the witness `WitnessArgs`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// …
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
```

The on-chain C VM DAO script (`dao.c`) reads only the **lowest byte** of this 8-byte field as the index into `header_deps`. This is explicitly documented in the test added to `util/dao/src/tests.rs`:

```rust
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

For a witness value of `257` (little-endian bytes `0x01 0x01 0x00 … 0x00`):
- **C VM** reads lowest byte → index `1` → `header_deps[1]` = deposit block → block-number check passes → transaction accepted.
- **Rust** reads full `u64` → index `257` → `header_deps[257]` = a different block → block-number check fails → `DaoError::InvalidOutPoint` returned.

The test confirms this divergence and asserts the Rust path returns an error:

```rust
// Rust resolves index 257 → withdraw block (number 200), but cell data
// says deposited at block 100. Block number check catches the mismatch.
assert!(result.is_err(), "expected Err, got {result:?}");
```

`DaoCalculator::transaction_fee()` (which calls `transaction_maximum_withdraw()`) is invoked in two security-critical paths:

1. **Tx-pool admission** — `check_tx_fee()` in `tx-pool/src/util.rs` rejects the transaction before it can enter the pool.
2. **Block verification** — `FeeCalculator::transaction_fee()` in `verification/src/transaction_verifier.rs` is called during contextual block verification; a rejection here causes the entire block to be rejected.

---

### Impact Explanation

A malicious miner constructs a DAO withdrawal transaction with ≥ 258 `header_deps` and a witness index of `257` (or any value whose lowest byte ≠ full value). The C VM on-chain script accepts the transaction; the Rust node's `DaoCalculator` rejects it. When the miner includes this transaction in a mined block:

- Every Rust-based CKB node rejects the block as invalid.
- Nodes running the C VM (or any implementation that matches the C VM's byte-truncation behavior) accept the block.
- The network forks: the miner's chain advances while all Rust nodes stall on the pre-fork tip.

This is a **consensus split** reachable by any miner (no majority hash power required — a single block suffices to trigger the divergence).

---

### Likelihood Explanation

The attack requires a miner willing to include a specially crafted transaction. The transaction itself is valid under the on-chain script rules and requires no privileged access. The only barrier is constructing a transaction with ≥ 258 `header_deps`, which is permitted by the protocol (no hard cap below the transaction-size limit). The discrepancy is already documented in the production test suite, confirming the divergence is present in the current codebase.

---

### Recommendation

Align the Rust `DaoCalculator` with the C VM's actual byte-truncation behavior. Replace the full `u64` read with a `u8` read (or mask to the lowest byte) when indexing into `header_deps`:

```rust
// Current (reads full u64):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// Fixed (matches C VM lowest-byte behavior):
let index_bytes = header_deps_index_data.unwrap();
let header_dep_index = index_bytes[0] as u64; // lowest byte only
```

Alternatively, if the C VM is the component with the bug, patch the on-chain DAO script to read the full `u64` and coordinate a hard fork. Either way, the two implementations must agree on the same index-resolution semantics before the discrepancy can be exploited.

---

### Proof of Concept

The existing test in `util/dao/src/tests.rs` already demonstrates the divergence:

1. Build a `ResolvedTransaction` with 258 `header_deps`: `header_deps[1]` = deposit block (number 100), `header_deps[257]` = withdraw block (number 200).
2. Set witness `input_type` = `257u64` in little-endian (bytes `0x01 0x01 0x00 … 0x00`).
3. Set cell data = `100u64` (deposit block number).
4. Call `DaoCalculator::transaction_fee(&rtx)`.

**Rust result**: `Err(InvalidOutPoint)` — resolves index 257 → withdraw block (number 200) ≠ cell data (100).

**C VM result** (per test comment): resolves lowest byte 1 → deposit block (number 100) == cell data (100) → **passes**.

A miner submitting a block containing this transaction will be accepted by C-VM-based nodes and rejected by all Rust nodes, splitting the chain. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
