### Title
DAO Calculator Reads Full u64 Header-Dep Index While On-Chain Script Reads Only Lowest Byte — (`util/dao/src/lib.rs`)

### Summary
`DaoCalculator::transaction_maximum_withdraw` reads the full 8-byte little-endian u64 from the witness `input_type` field and uses it directly as the `header_deps` array index. The on-chain DAO C script reads only the **lowest byte** of that same u64. A transaction with `input_type = 257` (0x0000000000000101 LE) is resolved as index 257 by Rust but as index 1 by the C VM. This creates a split: the Rust tx-pool rejects DAO withdrawal transactions that the on-chain script would accept, and may accept transactions the on-chain script would reject.

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the header-dep index from the witness:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

and immediately uses the full u64 to index into `header_deps`:

```rust
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)   // full u64 cast to usize
        .and_then(|hash| header_deps.get(&hash))
        .ok_or(DaoError::InvalidOutPoint)
})?;
```

The on-chain `dao.c` script reads only the **lowest byte** of the same 8-byte witness field. The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` explicitly documents this split:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

The test constructs a 258-entry `header_deps` list, places the deposit block at index 1 and the withdraw block at index 257, sets `input_type = 257`, and asserts `result.is_err()` — confirming Rust resolves index 257 (withdraw block), while the C VM resolves index 1 (deposit block) and would accept the transaction.

This is the direct analog of the ERC20 bug: a function receives an explicit "from" identity (`header_dep_index` from the witness) but internally uses a **different** identity (the full u64 vs. the lowest byte), causing the wrong reference to be resolved in the critical operation.

### Impact Explanation

**Split 1 — Rust rejects what the C VM accepts (DoS):** A valid DAO withdrawal transaction whose witness encodes `input_type = N` where `N > 255` and `N & 0xFF` points to the correct deposit block will be accepted by the on-chain script but rejected by the Rust `DaoCalculator`. Because `transaction_fee` is called during tx-pool admission, such transactions are silently dropped from every Rust node's mempool. A user crafting a legitimate DAO withdrawal with more than 255 `header_deps` cannot propagate it through the Rust P2P network.

**Split 2 — Rust accepts what the C VM rejects (incorrect fee accounting):** An attacker can craft a transaction where `header_deps[N]` (full index) is the correct deposit block but `header_deps[N & 0xFF]` (lowest-byte index) is not. The Rust `DaoCalculator` computes a valid fee and admits the transaction; the on-chain script rejects it. Miners relying on the Rust fee estimate would include a transaction that fails script execution, wasting block space and causing fee-accounting divergence.

### Likelihood Explanation

Any unprivileged tx-pool submitter can trigger Split 1 by constructing a DAO withdrawal transaction with ≥ 256 `header_deps` and setting `input_type` to an index whose lowest byte differs from the full value. The CKB protocol does not cap `header_deps` count below 256 (the test itself creates 258 entries). Split 2 requires the same construction with an additional crafted `