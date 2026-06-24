All six factual claims in the submission are verified against the actual code:

All six factual claims are verified against the actual code. The finding is real and the exploit path is clear. Here is my assessment:

---

Audit Report

## Title
Unnecessary Full Block Fetch in `complete_headers` Enables I/O Amplification via `GetLastStateProof` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
`BlockSampler::complete_headers` calls `snapshot.get_block()` for each sampled block number, which reads the full block body (all transactions) from `COLUMN_BLOCK_BODY` via `get_block_body()`. Only `calc_uncles_hash()` and `extension()` are consumed from the result — the entire transaction body is deserialized and allocated for nothing. The cheaper `get_block_uncles + get_block_extension` pattern used in `get_blocks_proof.rs` would suffice and avoids the body read entirely. Any unprivileged light-client peer can trigger this for up to 998 blocks per `GetLastStateProof` request.

## Finding Description
In `complete_headers` (lines 137–148 of `get_last_state_proof.rs`), for each block number in the `numbers` slice, the code calls `self.snapshot.get_block(&ancestor_header.hash())`. Internally, `get_block()` in `store/src/store.rs` (lines 55–68) calls `self.get_block_body(h)`, which iterates `COLUMN_BLOCK_BODY` via `get_iter`, deserializing every `TransactionView` in the block. It also calls `get_block_uncles`, `get_block_proposal_txs_ids`, and `get_block_extension`. Of the full `BlockView` constructed, only `ancestor_block.calc_uncles_hash()` (line 147) and `ancestor_block.extension()` (line 148) are used. The transaction body is read, deserialized, heap-allocated, and immediately discarded.

By contrast, `get_blocks_proof.rs` (lines 88–93) calls `snapshot.get_block_uncles(&block_hash)` and `snapshot.get_block_extension(&block_hash)` directly. Both are backed by `StoreCache`'s `block_uncles` and `block_extensions` LRU caches (confirmed in `store/src/cache.rs` lines 22–25). There is no LRU cache for block body data, so every `get_block()` call unconditionally hits RocksDB for `COLUMN_BLOCK_BODY`.

The limit check at line 201 is `difficulties.len() + (last_n_blocks as usize) * 2 > GET_LAST_STATE_PROOF_LIMIT` where `GET_LAST_STATE_PROOF_LIMIT = 1000`. With `difficulties = []` and `last_n_blocks = 499`, the check evaluates to `0 + 998 > 1000` → false, so it passes. `complete_headers` is then called with up to 998 block numbers, each triggering a full `COLUMN_BLOCK_BODY` scan.

## Impact Explanation
Each `get_block()` call reads all transactions for a block from RocksDB's `COLUMN_BLOCK_BODY`. For blocks near the CKB block size limit, this is hundreds of KB to several MB of data per block. With 998 blocks per request and no LRU cache for block bodies, a single peer forces the full node to read and deserialize up to ~998× the per-block transaction data compared to the `get_block_uncles + get_block_extension` path. Repeated requests from one or more peers multiply the disk I/O load. This constitutes a concrete, externally-triggerable performance degradation matching the allowed impact: **Low (501–2000 points) — Any other important performance improvements for CKB**.

## Likelihood Explanation
The path is reachable by any unprivileged light-client peer with no proof-of-work, no key, and no special role. The crafted message is trivially constructable: set `difficulties` to empty and `last_n_blocks` to 499. The limit check passes. No rate limiting or additional guard is present in the code path to prevent the full-block fetch loop from executing.

## Recommendation
Replace the `get_block()` call in `complete_headers` with the same pattern used in `get_blocks_proof.rs`:

```rust
// Instead of:
let ancestor_block = self.snapshot.get_block(&ancestor_header.hash())...;
let uncles_hash = ancestor_block.calc_uncles_hash();
let extension = ancestor_block.extension();

// Use:
let uncles = self.snapshot.get_block_uncles(&ancestor_header.hash())
    .ok_or_else(|| format!("failed to find uncles for header#{}", number))?;
let uncles_hash = uncles.calc_uncles_hash();
let extension = self.snapshot.get_block_extension(&ancestor_header.hash());
```

This eliminates the `COLUMN_BLOCK_BODY` read entirely and benefits from the existing `block_uncles` and `block_extensions` LRU caches in `StoreCache`.

## Proof of Concept
1. Connect a light-client peer to a full node running on a chain with at least 500 blocks.
2. Send a `GetLastStateProof` message with `difficulties = []`, `last_n_blocks = 499`, and a valid `last_hash` on the main chain.
3. The limit check at line 201 passes: `0 + 499*2 = 998 ≤ 1000`.
4. `complete_headers` is called with up to 998 block numbers.
5. For each, `snapshot.get_block()` reads `COLUMN_BLOCK_BODY` from RocksDB.
6. Instrument RocksDB's `COLUMN_BLOCK_BODY` read byte counter before and after the request. Compare against the same request handled via the `get_block_uncles + get_block_extension` path. The `get_block()` path will show reads proportional to total transaction data across those 998 blocks; the targeted path will show zero `COLUMN_BLOCK_BODY` reads.