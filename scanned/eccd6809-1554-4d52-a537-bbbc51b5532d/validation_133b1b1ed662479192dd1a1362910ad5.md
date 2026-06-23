### Title
DAO Withdrawal Tx-Pool Rejection via `header_dep_index` Interpretation Discrepancy Between Rust `DaoCalculator` and On-Chain C VM — (`util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the `header_dep_index` from the witness as a full little-endian `u64`, while the on-chain C VM `dao.c` reads only the **lowest byte** of that same 8-byte field. When a transaction encodes an index whose value exceeds 255 (e.g., 257), the two implementations resolve different entries in `header_deps`. The Rust path then fails its own block-number cross-check and rejects the transaction, even though the C VM would accept it as valid. This creates a tx-pool DoS path: a valid DAO phase-2 withdrawal can be permanently blocked from entering the mempool.

---

### Finding Description

In `transaction_maximum_withdraw`, the deposit-block header is located by reading the witness `input_type` field as a full `u64` index:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)   // ← full u64 used as slice index
        .and_then(|hash| header_deps.get(&hash))
        .ok_or(DaoError::InvalidOutPoint)
})?;
```

The on-chain `dao.c` script (referenced at `test/src/specs/dao/dao_user.rs:14`) reads only the lowest byte of the same 8-byte field. For any index value whose lowest byte differs from the full value (i.e., index > 255), the two implementations resolve different `header_deps` slots.

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` explicitly documents this split:

```
// Position 1: