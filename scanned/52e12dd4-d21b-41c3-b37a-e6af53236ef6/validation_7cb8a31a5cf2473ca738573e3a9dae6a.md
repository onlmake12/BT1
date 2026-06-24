Audit Report

## Title
Unnecessary `COLUMN_BLOCK_BODY` Reads in `BlockSampler::complete_headers` via `get_block()` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
`BlockSampler::complete_headers` calls `self.snapshot.get_block()` for every sampled block number, which unconditionally reads `COLUMN_BLOCK_BODY` (all serialized transactions) from RocksDB, but only consumes `calc_uncles_hash()` and `extension()` from the result. The transaction body data is loaded into memory and immediately dropped. The sibling handler `get_blocks_proof.rs` correctly avoids this by calling `get_block_header` + `get_block_uncles` + `get_block_extension` individually. Any unprivileged light-client peer can trigger this amplified I/O path by sending a valid `GetLastStateProof` message.

## Finding Description
In `complete_headers` (lines 137–148 of `get_last_state_proof.rs`), `self.snapshot.get_block(&ancestor_header.hash())` is called for every block number in the sampled set:

```rust
let ancestor_block = self
    .snapshot
    .get_block(&ancestor_header.hash())  // reads full block including all txs
    ...?;
let uncles_hash = ancestor_block.calc_uncles_hash();  // only field used
let extension = ancestor_block.extension();            // only field used
```

`get_block()` in `store/src/store.rs` (line 55) calls `self.get_block_body(h)` for non-frozen blocks, which iterates `COLUMN_BLOCK_BODY` and deserializes every `TransactionView` in the block. For frozen blocks the entire block is read from the freezer, which also includes all transactions. Neither path avoids reading transaction data.

By contrast, `get_blocks_proof.rs` (lines 81–95) performs the identical logical operation — building `uncles_hash` and `extension` — using only:
- `snapshot.get_block_header(&block_hash)`
- `snapshot.get_block_uncles(&block_hash)`
- `snapshot.get_block_extension(&block_hash)`

…with zero reads from `COLUMN_BLOCK_BODY`.

The `GET_LAST_STATE_PROOF_LIMIT = 1000` guard (line 201–204) bounds the *count* of sampled blocks, not the *bytes read per sample*. On a chain with large blocks, each `get_block()` call reads O(avg_block_body_size) bytes of unnecessary data.

**Note on the `last_block` claim:** The report's assertion that `last_block` (line 216–218) "only uses `last_block.number()`" is factually incorrect. `last_block` is passed to `reply_proof` (line 374), where `lib.rs` (lines 221–223) uses `last_block.data().header()`, `last_block.calc_uncles_hash()`, and `last_block.extension()` to build the `VerifiableHeader`. The `get_block()` call for `last_block` is therefore legitimately needed (though it could still be replaced with `get_block_header` + `get_block_uncles` + `get_block_extension`). The primary valid finding is confined to `complete_headers`.

## Impact Explanation
This is a suboptimal implementation of the CKB state storage access pattern, matching **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**. With `GET_LAST_STATE_PROOF_LIMIT = 1000`, a single `GetLastStateProof` request can trigger up to 1000 unnecessary `COLUMN_BLOCK_BODY` scans. On a chain with high-throughput blocks (e.g., 1000 txs × ~500 bytes = ~500 KB per block), one request causes up to ~500 MB of unnecessary RocksDB reads and deserialization work. This amplifies I/O and memory pressure on the full node proportionally to block body size, not just block count, and is avoidable by using the same targeted API calls already used in `get_blocks_proof.rs`.

## Likelihood Explanation
Any peer that can send light-client protocol messages can trigger this. No PoW, no privileged role, no key material required. The request is structurally valid and passes all existing guards before reaching `complete_headers`. The attack is repeatable at will.

## Recommendation
Replace `get_block()` in `complete_headers` with the targeted pattern already used in `get_blocks_proof.rs`:

```rust
// Instead of:
let ancestor_block = self.snapshot.get_block(&ancestor_header.hash())?;
let uncles_hash = ancestor_block.calc_uncles_hash();
let extension = ancestor_block.extension();

// Use:
let uncles = self.snapshot.get_block_uncles(&ancestor_header.hash())
    .ok_or_else(|| format!(...))?;
let uncles_hash = uncles.data().calc_uncles_hash();
let extension = self.snapshot.get_block_extension(&ancestor_header.hash());
```

This eliminates all `COLUMN_BLOCK_BODY` reads in the `complete_headers` loop. For the `last_block` fetch, replacing `get_block()` with `get_block_header` + `get_block_uncles` + `get_block_extension` would also avoid the body read, though the body is not used in `reply_proof`.

## Proof of Concept
Differential benchmark:
1. Populate a chain with blocks containing 1000 transactions each.
2. Instrument `ChainStore::get_block_body` to count invocations and bytes read.
3. Send a valid `GetLastStateProof` message with `difficulties` and `last_n_blocks` set to maximize sampled block count (up to `GET_LAST_STATE_PROOF_LIMIT = 1000`).
4. Assert `get_block_body` is called N times (once per sampled block) in the `complete_headers` path.
5. Apply the fix (use `get_block_header` + `get_block_uncles` + `get_block_extension`).
6. Assert `get_block_body` is called 0 times for the same request.
7. Measure bytes read from `COLUMN_BLOCK_BODY`: before fix = O(N × avg_block_body_size); after fix = 0.