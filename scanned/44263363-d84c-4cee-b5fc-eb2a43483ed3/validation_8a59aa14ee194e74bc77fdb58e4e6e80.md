### Title
DAO Withdrawal `header_dep_index` Parsed as Full `u64` in Rust but Lowest Byte Only in On-Chain `dao.c` — (File: `util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the `header_dep_index` from `WitnessArgs.input_type` as a full 8-byte little-endian `u64`, while the on-chain `dao.c` script reads only the lowest byte of that same 8-byte field (treating it as a `u8`). For any index value > 255, the two implementations resolve to different entries in `header_deps`, causing the Rust host and the on-chain script to disagree on which deposit block header to use. This inconsistency is the direct CKB analog of the reported EIP-712 `DOMAIN_TYPEHASH` field mismatch: a declared schema and the actual runtime interpretation diverge, producing silent disagreement between two components that must agree.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit-header index from the witness:

```rust
// Line 91
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
// Line 96
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // full u64 used as array index
```

The comment on line 79 states: *"dao contract stores header deps index as u64 in the input_type field of WitnessArgs"*. However, the on-chain `dao.c` binary reads only `input_type[0]` — the lowest byte — as the index. This is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8` (`util/dao/src/tests.rs`, lines 489–491):

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

For any `header_dep_index` whose value exceeds 255 (e.g., 257 = `0x0101_0000_0000_0000` in LE):
- **Rust** resolves `header_deps[257]`
- **dao.c** resolves `header_deps[1]` (lowest byte = 1)

These are different array slots and therefore potentially different block hashes and different accumulate-rate (`ar`) values.

The Rust code has a partial guard at line 105:
```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
```
This catches the common case where the two slots hold blocks with different block numbers. However, it does **not** catch the case where both slots hold blocks with the same block number but different `ar` values (e.g., two competing fork blocks at the same height that are both present in `header_deps`). In that scenario the block-number check passes in Rust, Rust accepts the transaction and computes a maximum-withdraw based on the `ar` of `header_deps[257]`, while `dao.c` computes a different maximum-withdraw based on the `ar` of `header_deps[1]`.

---

### Impact Explanation

**Primary — Denial-of-service / invalid block template (medium)**

A transaction sender who submits a DAO withdrawal with `header_dep_index > 255` and the deposit block placed at the lowest-byte slot (so `dao.c` accepts it) will have the transaction rejected by the Rust `DaoCalculator` fee check (because the block-number guard fires). The node therefore refuses to relay or mine a transaction that is fully valid on-chain. This is a reachable, attacker-controlled denial-of-service against any DAO depositor who constructs such a transaction.

**Secondary — Incorrect DAO field in mined blocks (high if triggered)**

`dao_field_with_current_epoch` calls `withdrawed_interests`, which calls `transaction_maximum_withdraw`. If a DAO withdrawal with `header_dep_index > 255` reaches a block (e.g., via a miner that bypasses the Rust fee check, or in a fork-block scenario where the block-number guard does not fire), the `ar`-based interest calculation uses the wrong header. The resulting `s` component of the DAO field is wrong. Every subsequent DAO withdrawal that relies on this field will compute incorrect interest, and nodes that re-derive the DAO field independently will disagree — a consensus split.

---

### Likelihood Explanation

The attacker-controlled entry path is the standard `send_transaction` RPC or P2P transaction relay. No privileged role is required. The attacker only needs to:
1. Hold a DAO deposit.
2. Construct a withdrawal transaction with ≥ 258 `header_deps` entries, placing the deposit block at position `(index & 0xFF)` and any other valid canonical header at position `index`.
3. Set `WitnessArgs.input_type` to the crafted 8-byte LE index.

The block-number guard mitigates the most obvious exploitation path, but the underlying schema mismatch remains and is reachable without any privileged access. Likelihood is **medium** for the DoS path and **low-to-medium** for the incorrect-DAO-field path (requires a fork-block or miner bypass).

---

### Recommendation

Align the Rust `DaoCalculator` with the on-chain `dao.c` interpretation. If `dao.c` reads only the lowest byte, the Rust code should do the same:

```rust
// util/dao/src/lib.rs  — replace line 91
let index_bytes = header_deps_index_data.unwrap();
Ok(index_bytes[0] as u64)   // match dao.c: lowest byte only
```

Alternatively, update `dao.c` to read the full 8-byte little-endian `u64` and add a consensus-level cap (e.g., reject transactions where `header_dep_index ≥ header_deps.len()` or `≥ 256`) so both sides are unambiguous. Add an explicit upper-bound validation on the index in the Rust path regardless of which fix is chosen.

---

### Proof of Concept

1. Create a DAO deposit at block 100.
2. Build a withdrawal transaction with 258 `header_deps`:
   - `header_deps[1]` = deposit block hash (block 100) — what `dao.c` resolves via lowest byte of index 257
   - `header_deps[257]` = any other canonical block with a different block number — what Rust resolves via full u64
3. Set `WitnessArgs.input_type` = `257u64.to_le_bytes()` (8 bytes: `[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`