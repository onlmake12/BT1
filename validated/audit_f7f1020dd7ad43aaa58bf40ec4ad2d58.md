### Title
DAO Withdrawal Tx-Pool DoS via Witness Header-Dep Index Exceeding u8 — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` reads the deposit-header index from the witness as a full `u64`, while the on-chain C VM DAO script resolves the same field using only the lowest byte. When a DAO withdrawal transaction carries a witness index > 255, the two implementations resolve different `header_deps` entries. The Rust tx-pool then rejects the transaction via a block-number mismatch, even though the C VM would accept it — a DoS against the DAO withdrawer.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit-header index from the witness `input_type` field as a full 8-byte little-endian `u64`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then uses that value directly to index into `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
    .and_then(|hash| header_deps.get(&hash))
    .ok_or(DaoError::InvalidOutPoint)
```

The on-chain C VM DAO script (referenced at `test/src/specs/dao/dao_user.rs` line 14 and documented in the test at `util/dao/src/tests.rs` lines 490–491) resolves the same field using **only the lowest byte** of the u64 — effectively treating it as a `u8`. When a transaction is crafted with a witness index whose full u64 value differs from its lowest-byte value (any value > 255, e.g. 257 = `0x0000000000000101`), the Rust code and the C VM resolve **different** entries in `header_deps`.

The Rust block-number cross-check at line 105:

```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
```

then fails because the Rust-resolved header is not the actual deposit header. This error propagates through `check_tx_fee` in `tx-pool/src/util.rs`:

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

causing the tx pool to emit `Reject::Malformed` and permanently drop the transaction. The C VM, using the correct lowest-byte index, would validate the same transaction successfully.

The same `transaction_maximum_withdraw` is also called from `withdrawed_interests` → `dao_field_with_current_epoch` (line 222), meaning the discrepancy also affects the DAO field computed during block assembly.

---

### Impact Explanation

A DAO withdrawer whose phase-2 withdrawal transaction carries a witness `input_type` index > 255 will have their transaction permanently rejected by every honest node's tx pool, even though the transaction is consensus-valid and would be accepted by the C VM DAO script. The user cannot complete their DAO withdrawal through the normal tx-pool path. This is a DoS against the DAO withdrawal operation.

Additionally, because `dao_field_with_current_epoch` calls `withdrawed_interests` → `transaction_maximum_withdraw`, if such a transaction were included in a block via a path that bypasses the tx pool, the Rust node would compute an incorrect `current_s` (NervosDAO secondary issuance accumulator) in the DAO field, potentially causing a consensus split between nodes.

---

### Likelihood Explanation

A transaction sender (DAO withdrawer) fully controls the witness field and can craft a withdrawal transaction with any u64 index value. While a legitimate user would normally use a small index (< 256), the discrepancy is a latent correctness bug: any wallet or tooling that constructs DAO withdrawal transactions with more than 256 `header_deps` and uses a high index would silently produce transactions that are rejected by the tx pool but valid on-chain. The entry path is the standard `send_transaction` RPC or P2P relay — no privileged access is required.

---

### Recommendation

In `transaction_maximum_withdraw` (`util/dao/src/lib.rs`), validate that the witness `input_type` index fits within a `u8` and return `DaoError::InvalidDaoFormat` if it does not, or align the Rust implementation with the C VM by masking to the lowest byte:

```rust
let index = LittleEndian::read_u64(&header_deps_index_data.unwrap());
if index > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
```

This makes the Rust tx-pool and block verifier consistent with the C VM DAO script's index resolution.

---

### Proof of Concept

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–537) directly demonstrates this. It constructs a DAO withdrawal transaction with witness index 257, places the deposit block at `header_deps[1]` (what the C VM resolves via lowest byte) and the withdraw block at `header_deps[257]` (what Rust resolves via full u64), and asserts that `DaoCalculator::transaction_fee` returns an error — confirming that the Rust tx-pool rejects a transaction the C VM would accept:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
...
// Rust resolves index 257 → withdraw block (number 200), but cell data
// says deposited at block 100. Block number check catches the mismatch.
assert!(result.is_err(), "expected Err, got {result:?}");
```

The root cause is in production code: [1](#0-0) 

Called from the tx-pool fee check: [2](#0-1) 

And from block DAO field computation: [3](#0-2)

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

**File:** util/dao/src/lib.rs (L222-222)
```rust
        let withdrawed_interests = self.withdrawed_interests(rtxs)?;
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
