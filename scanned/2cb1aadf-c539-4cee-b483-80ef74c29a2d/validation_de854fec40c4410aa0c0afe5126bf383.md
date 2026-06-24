Audit Report

## Title
DAO Withdrawal `header_deps` Index Parsed as `u64` in Rust vs. Effective `u8` in On-Chain C Script — Consensus Split via Incorrect Header Resolution (`util/dao/src/lib.rs`)

## Summary

`DaoCalculator::transaction_maximum_withdraw()` reads the `header_deps` index from the DAO withdrawal witness `input_type` field as a full 8-byte little-endian `u64`, while the on-chain DAO C script reads only the lowest byte (effectively `u8`). When a transaction encodes an index value greater than 255, the Rust node and the C VM resolve different block headers. A miner can include such a transaction in a block that passes C VM script execution but is rejected by the Rust node's DAO field verification, causing a consensus split.

## Finding Description

In `util/dao/src/lib.rs` at line 91, `transaction_maximum_withdraw()` parses the deposit block's position in `header_deps` from the witness `input_type` field as a full `u64`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

The on-chain DAO C script reads only the lowest byte of the same 8-byte field — effectively treating it as a `u8`. This behavioral difference is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–537), which constructs a transaction with 258 `header_deps`, places the deposit block at position 1 and the withdraw block at position 257, and encodes `input_type = 257u64` (LE bytes: `[0x01, 0x01, 0x00, ...]`, lowest byte = 1):

- **C VM** reads lowest byte → index 1 → deposit block (number 100) → matches cell data → **accepts**
- **Rust** reads full u64 → index 257 → withdraw block (number 200) → number 200 ≠ cell data 100 → **rejects** (`DaoError::InvalidOutPoint`)

The test asserts `result.is_err()`, confirming the Rust node rejects what the C VM accepts.

The `DaoCalculator` is invoked in two critical paths:
1. **Tx-pool admission** (`transaction_fee` → `transaction_maximum_withdraw`): the crafted withdrawal is rejected at relay/admission even though the on-chain script would accept it.
2. **Block verification** (`dao_field_with_current_epoch` → `withdrawed_interests` → `transaction_maximum_withdraw`): a miner who includes such a transaction produces a block whose DAO field the Rust verifier computes incorrectly, causing the block to be rejected by Rust nodes even though C VM script execution succeeds.

Existing guards (the 8-byte length check at lines 85–90) do not prevent this — they only validate the field length, not the range of the index value.

## Impact Explanation

**Critical — consensus deviation.** A miner can craft a DAO withdrawal transaction with more than 256 `header_deps` entries and a witness index > 255. The on-chain DAO C script accepts the transaction (resolves the correct deposit header via the lowest byte), but the Rust `DaoCalculator` resolves a different header and rejects the block at the DAO field verification step. This causes a chain fork between nodes executing the C VM and nodes running the Rust verifier, directly matching the "Vulnerabilities which could easily cause consensus deviation" Critical impact class.

## Likelihood Explanation

The CKB protocol imposes no hard limit on the number of `header_deps` entries beyond block size limits, so constructing a transaction with 258 `header_deps` is feasible for any transaction sender. A miner can include such a transaction directly without going through the tx-pool. The discrepancy is already documented in the production test suite (test name `check_dao_withdraw_header_dep_index_exceeds_u8`, lines 475–537), confirming developer awareness of the behavioral difference. The trigger condition (index > 255, ≥ 258 `header_deps`) is straightforward to construct.

## Recommendation

**Short term:** Add a range check in `transaction_maximum_withdraw()` immediately after parsing the index:

```rust
if header_dep_index > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
```

This aligns Rust behavior with the C VM's effective `u8` read and prevents the divergence.

**Long term:** Audit the on-chain DAO C script to formally document the exact width used to read the index. If large `header_deps` lists are intended to be supported, upgrade the DAO script to explicitly use a `u64` index so both sides agree. Update the RFC accordingly.

## Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–537) directly demonstrates the Rust-side rejection. To demonstrate the full consensus split:

1. Craft a DAO withdrawal transaction:
   - `header_deps`: 258 entries; position 1 = deposit block hash (block 100), position 257 = withdraw block hash (block 200), rest = dummy hashes.
   - Cell data: `100u64` as LE bytes (deposit block number).
   - Witness `input_type`: `257u64` as 8-byte LE (`[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`).
2. Submit to C VM: lowest byte of index = `0x01` → resolves `header_deps[1]` = deposit block (number 100) → matches cell data → script execution **succeeds**.
3. Miner includes this transaction in a block; Rust node calls `dao_field_with_current_epoch` → `withdrawed_interests` → `transaction_maximum_withdraw` → reads full u64 = 257 → resolves `header_deps[257]` = withdraw block (number 200) → number 200 ≠ cell data 100 → `DaoError::InvalidOutPoint` → **block rejected**.
4. Result: block is valid per C VM but invalid per Rust node → consensus split.