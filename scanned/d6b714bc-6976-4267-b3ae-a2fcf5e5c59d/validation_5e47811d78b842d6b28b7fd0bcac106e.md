### Title
DAO Withdrawal Header-Dep Index Resolved as `u64` in Rust vs. Lowest Byte in C VM — Inconsistent Validation Causes Unintended Tx-Pool Rejection and Potential Consensus Split (`util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the witness `input_type` field as a full `u64` to index into `header_deps`, while the on-chain C VM implementation (`dao.c`) reads only the **lowest byte** of that same field. When a DAO withdrawal transaction encodes a header-dep index whose value exceeds 255 (i.e., the high bytes are non-zero), the two implementations resolve **different** header entries. This causes the Rust tx-pool to reject transactions that the C VM would accept, and can produce a consensus split if such a transaction is included in a block.

---

### Finding Description

**Root cause — `util/dao/src/lib.rs`, lines 83–99:** [1](#0-0) 

```rust
let header_deps_index_data: Option<Bytes> =
    witness.input_type().to_opt().map(|witness| witness.into());
if header_deps_index_data.is_none()
    || header_deps_index_data.clone().map(|data| data.len()) != Some(8)
{
    return Err(DaoError::InvalidDaoFormat);
}
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))   // ← full u64
```

followed by:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // ← uses full u64 as index
```

The C VM (`dao.c`) reads the same 8-byte field but uses only its **lowest byte** as the index into `header_deps`. This is documented in the test file itself: [2](#0-1) 

```
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
header_deps[1] = deposit_block.hash();
header_deps[257] = withdraw_block.hash();
```

**Exploit path:**

A transaction sender crafts a valid DAO withdrawal where:
- `header_deps[1]` = the correct deposit block hash
- `header_deps[257]` = any other block hash (e.g., the withdraw block)
- `witness.input_type` = `257u64` in little-endian (bytes: `0x01, 0x01, 0x00, …`)

The C VM reads byte 0 = `0x01` → resolves `header_deps[1]` = deposit block → block number matches cell data → **script execution succeeds**.

The Rust `DaoCalculator` reads the full `u64` = `257` → resolves `header_deps[257]` = wrong block → block number mismatch at line 105 → **returns `DaoError::InvalidOutPoint`**. [3](#0-2) 

```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
```

The test at line 476 confirms this divergence and asserts `result.is_err()`: [4](#0-3) 

**Tx-pool rejection path:**

`check_tx_fee` in `tx-pool/src/util.rs` calls `DaoCalculator::transaction_fee`, which calls `transaction_maximum_withdraw`. A `DaoError` is mapped to `Reject::Malformed`, causing the tx-pool to permanently reject the transaction. [5](#0-4) 

**Block verification path:**

`DaoHeaderVerifier::verify` calls `DaoCalculator::dao_field`, which iterates resolved inputs via `transaction_maximum_withdraw`. If a block containing such a transaction is submitted, the Rust verifier computes a different DAO accumulator value than the C VM, causing the Rust node to reject the block with `BlockErrorKind::InvalidDAO`. [6](#0-5) 

---

### Impact Explanation

1. **Unintended tx-pool rejection (DoS on DAO withdrawals):** Any DAO withdrawal transaction whose witness encodes a header-dep index > 255 is permanently rejected by the Rust tx-pool even though the C VM would accept it. Affected users cannot withdraw their DAO deposits through the normal submission path.

2. **Consensus split:** If a miner assembles a block containing such a transaction (bypassing the tx-pool, e.g., via direct block template injection), Rust nodes reject the block (`InvalidDAO`) while C-VM-based nodes accept it. This splits the network.

---

### Likelihood Explanation

- The attacker is an unprivileged transaction sender with a DAO deposit cell.
- Constructing a transaction with `header_deps` padded to ≥ 258 entries and a witness index of 257 requires no special privilege — it is a valid molecule-encoded transaction.
- The C VM behavior (lowest-byte indexing) is a known property of `dao.c`; the test comments confirm the developers are aware of the divergence.
- The scenario is reachable via the standard `send_transaction` RPC.

---

### Recommendation

Align the Rust `DaoCalculator` with the C VM by reading only the lowest byte of the 8-byte witness index field, or enforce that the index fits in a `u8` and return `DaoError::InvalidDaoFormat` for values > 255. The fix belongs in `util/dao/src/lib.rs` at the point where `LittleEndian::read_u64` is called:

```rust
// Current (reads full u64):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// Fixed (mirrors C VM lowest-byte behavior):
let raw = LittleEndian::read_u64(&header_deps_index_data.unwrap());
if raw > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
Ok(raw)
```

Alternatively, if the protocol intends to support indices > 255, the C VM (`dao.c`) must be updated to read the full `u64` and the change must be activated via a hard fork.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–536) directly demonstrates the divergence. It constructs a transaction where:

- `header_deps[1]` = deposit block (number 100)
- `header_deps[257]` = withdraw block (number 200)
- `witness.input_type` = `257u64` LE
- Cell data = `100u64` LE (deposited at block 100)

The C VM resolves index `1` (lowest byte of `257`) → deposit block 100 → matches → **would accept**.
The Rust `DaoCalculator` resolves index `257` → withdraw block 200 → 200 ≠ 100 → **rejects with `DaoError::InvalidOutPoint`**. [7](#0-6) 

Run with:
```
cargo test -p ckb-dao check_dao_withdraw_header_dep_index_exceeds_u8
```

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

**File:** tx-pool/src/util.rs (L34-41)
```rust
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-319)
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
```
