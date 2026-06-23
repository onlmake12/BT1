### Title
NervosDAO Withdrawal `header_dep_index` Interpretation Discrepancy Between Rust Node and C VM Causes Consensus Split — (`File: util/dao/src/lib.rs`)

---

### Summary

The Rust node's `transaction_maximum_withdraw` function reads the DAO withdrawal `header_dep_index` from the witness as a full `u64`, while the on-chain C VM script (`dao.c`) reads only the lowest byte (effectively a `u8`). For any `header_dep_index >= 256`, the two runtimes resolve to different deposit block headers, creating a consensus split: the C VM accepts the withdrawal but the Rust node rejects it, permanently blocking legitimate DAO withdrawals with that index shape.

---

### Finding Description

The NervosDAO two-phase withdrawal protocol requires the withdrawing transaction to embed, in its witness, a `u64` little-endian index into `header_deps` that identifies the original deposit block. The Rust node's fee/reward calculator reads this index with:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

and then uses the full `u64` value to index into `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
``` [1](#0-0) 

The on-chain C VM script (`dao.c`) reads the same 8-byte field but interprets only the lowest byte as the index (i.e., `index = witness_byte[0]`). For any witness value whose full `u64` differs from its lowest byte — i.e., any value `>= 256` — the two runtimes resolve to entirely different entries in `header_deps`.

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` explicitly documents this split:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
// input_type = 257, lowest byte = 1
``` [2](#0-1) 

When `header_dep_index = 257` (little-endian bytes `[0x01, 0x01, 0x00, …]`):
- **C VM** reads byte `[0]` = `0x01` → resolves `header_deps[1]` = deposit block ✓
- **Rust node** reads full `u64` = `257` → resolves `header_deps[257]` = a different block ✗

The Rust node then checks whether the resolved header's block number matches the block number stored in the cell data:

```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
``` [3](#0-2) 

Because the wrong block is resolved, the block-number check fails and the Rust node returns `DaoError::InvalidOutPoint`. This error propagates through `txs_fees` into `block_reward_internal`:

```rust
let txs_fees = self.txs_fees(target)?;
``` [4](#0-3) 

causing the entire block's reward calculation to fail and the block to be rejected by every Rust node — even though the C VM script itself would pass.

---

### Impact Explanation

Any DAO withdrawal transaction whose witness encodes `header_dep_index >= 256` is permanently unincludable in any block accepted by the Rust reference node, regardless of whether the C VM script would accept it. Because the Rust node's reward calculator is invoked during block verification, a block containing such a transaction fails verification on every honest node. The depositor's CKB is locked in the DAO cell with no valid withdrawal path through the standard node software.

Additionally, the discrepancy constitutes a consensus split between the C VM (the authoritative script executor) and the Rust node's fee/reward accounting layer: the two components disagree on which historical block header is the "deposit snapshot," directly analogous to the external report's missing-snapshot class where historical state integrity is violated.

---

### Likelihood Explanation

A transaction with 256 or more `header_deps` is unusual but not prohibited. Each `header_dep` is 32 bytes; 256 entries consume only 8 KB, well within the 512 KB transaction size limit. A user who constructs a multi-input DAO withdrawal aggregating many deposit epochs, or who adds unrelated header deps for script purposes, could reach this threshold. The entry path requires only a standard RPC `send_transaction` call — no privileged access.

---

### Recommendation

Align the Rust node's index parsing with the C VM's behavior. Either:

1. **Truncate to `u8`**: After reading the `u64`, mask to the lowest byte: `header_dep_index & 0xFF`, matching `dao.c`'s actual behavior.
2. **Enforce an upper bound**: Reject any `header_dep_index >= 256` (or `>= header_deps.len()`) with a clear `InvalidDaoFormat` error before the block-number check, so the failure mode is explicit and consistent with the C VM's constraint.

Additionally, add a consensus-layer check that bounds `header_dep_index` to `u8::MAX` during transaction admission in the tx-pool, so malformed transactions are rejected at submission time rather than silently failing during block verification.

---

### Proof of Concept

The existing unit test in the repository directly demonstrates the split:

```rust
// header_deps[1]   = deposit_block  (C VM resolves here via byte[0] = 0x01)
// header_deps[257] = withdraw_block (Rust resolves here via full u64 = 257)
// witness input_type = 257u64 LE → byte[0] = 0x01

let witness = WitnessArgs::new_builder()
    .input_type(Some(Bytes::from(257u64.to_le_bytes().to_vec())))
    .build();
// ...
let result = calculator.transaction_fee(&rtx);
// Rust resolves index 257 → withdraw block (number 200),
// but cell data says deposited at block 100 → Err(InvalidOutPoint)
assert!(result.is_err(), "expected Err, got {result:?}");
``` [5](#0-4) 

A real attacker or an ordinary user with `header_dep_index >= 256` submits the transaction via `send_transaction` RPC. The C VM (running `dao.c`) resolves the correct deposit block and passes script verification. The Rust reward calculator resolves the wrong block, fails the block-number check, and causes every block containing the transaction to fail verification — permanently freezing the depositor's funds.

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

**File:** util/dao/src/lib.rs (L105-107)
```rust
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
```

**File:** util/dao/src/tests.rs (L489-495)
```rust
    // Pad header_deps to 258 entries so index 257 is valid.
    // Position 1: correct deposit block (what C VM resolves via lowest byte).
    // Position 257: withdraw block (wrong — Rust resolves this with full u64).
    let dummy = h256!("0x1").into();
    let mut header_deps = vec![dummy; 258];
    header_deps[1] = deposit_block.hash();
    header_deps[257] = withdraw_block.hash();
```

**File:** util/dao/src/tests.rs (L512-536)
```rust
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

**File:** util/reward-calculator/src/lib.rs (L103-105)
```rust
        let txs_fees = self.txs_fees(target)?;
        let proposal_reward = self.proposal_reward(parent, target)?;
        let (primary, secondary) = self.base_block_reward(target)?;
```
