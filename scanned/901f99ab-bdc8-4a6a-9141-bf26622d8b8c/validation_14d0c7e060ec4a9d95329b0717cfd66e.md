Audit Report

## Title
Unbounded `pending_compact_blocks` Growth via Rate-Limit-Exempt CompactBlock Relay Spam — (`File: sync/src/relayer/mod.rs`, `sync/src/relayer/compact_block_process.rs`)

## Summary
The `Relayer` handler unconditionally skips the per-peer rate limiter for every `CompactBlock` message, relying solely on PoW validity as an admission gate. When a compact block cannot be reconstructed due to missing transactions, it is inserted into the shared `pending_compact_blocks` map with no size cap. Because the only eviction path fires exclusively on successful block acceptance, an attacker with mining capability can continuously relay valid-PoW uncle/fork blocks whose transactions are absent from the victim's mempool, growing the map without bound for the duration of an epoch and exhausting node memory.

## Finding Description
**Root cause 1 — Rate limiter bypass:**
In `sync/src/relayer/mod.rs` at lines 112–114, `CompactBlock` messages are explicitly excluded from the `RateLimiter<(PeerIndex, u32)>` check (capped at 30 req/s for all other message types). The comment reads: *"CompactBlock will be verified by POW, it's OK to skip rate limit checking."* This means a peer can submit `CompactBlock` messages at an unlimited rate; the only admission gate is PoW header validity.

**Root cause 2 — Unbounded map insertion:**
`PendingCompactBlockMap` is defined in `sync/src/types/mod.rs` (lines 980–987) as a plain `HashMap<Byte32, (CompactBlock, HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>, u64)>` initialized with no size limit (line 1022). When reconstruction fails (`ReconstructionResult::Missing` or `ReconstructionResult::Collided`), `missing_or_collided_post_process` (lines 354–361 of `compact_block_process.rs`) inserts the compact block unconditionally via `.entry(...).or_insert_with(...)` — no capacity check precedes the insertion.

**Cleanup path is insufficient:**
The only eviction logic (lines 106–117 of `compact_block_process.rs`) executes inside the `ReconstructionResult::Block` branch and uses `retain` to drop entries from epochs older than the successfully accepted block. Uncle blocks and stale fork blocks are never accepted on the main chain, so this branch never fires for them. Entries accumulate for the entire epoch (~4 hours, ~1800 blocks at current epoch length).

**Deduplication does not bound growth:**
The `contextual_check` at lines 285–291 of `compact_block_process.rs` only rejects a message if the *same peer* sends the *same block hash* twice. Each newly mined block has a unique hash, so every crafted block creates a fresh map entry.

**Exploit flow:**
1. Attacker connects to victim as a normal P2P peer.
2. Attacker mines uncle-eligible blocks (valid PoW, parent = recent tip or ancestor), each containing one transaction `T` absent from the victim's mempool.
3. Attacker sends each as a `CompactBlock` message — rate limiter is skipped (lines 112–114, `mod.rs`).
4. `non_contextual_check` and `contextual_check` pass (valid PoW header, block within epoch window).
5. `reconstruct_block` returns `ReconstructionResult::Missing` because `T` is not in the tx-pool.
6. `missing_or_collided_post_process` inserts the full compact block into `pending_compact_blocks` (lines 354–361, `compact_block_process.rs`).
7. Because the blocks are uncle/stale, they are never accepted; the `retain` cleanup never fires.
8. Repeat for the epoch duration. The map grows proportionally to `(number_of_crafted_blocks) × (compact_block_size)`.

## Impact Explanation
Unbounded memory growth in `pending_compact_blocks` leads to OOM and node crash. This matches the allowed High impact: **"Vulnerabilities which could easily crash a CKB node."** The crash is deterministic once the attacker's mining output exceeds available node memory; no race condition or timing dependency is required.

## Likelihood Explanation
The attack requires valid PoW, which is a meaningful economic barrier. However, a miner with even 1–5% of network hashpower naturally produces multiple uncle-eligible blocks per epoch. Uncle blocks carry valid PoW and pass all header checks (`non_contextual_check`, `HeaderVerifier`). The attacker needs no privileged access, leaked keys, or social engineering — only a P2P connection and modest mining capability. The attack is sustained and repeatable for the full epoch duration. Likelihood: **Medium**.

## Recommendation
1. **Remove the CompactBlock rate-limit exemption** in `sync/src/relayer/mod.rs` lines 112–114, or apply a separate, lower quota (e.g., 2–5 per second per peer) for `CompactBlock` messages. PoW validity is not a substitute for submission-frequency control.
2. **Enforce a maximum size on `pending_compact_blocks`** in `missing_or_collided_post_process` (`compact_block_process.rs` lines 354–361). Before inserting, check `map.len()` against a configurable cap (e.g., 64 entries). When the cap is reached, evict the oldest entry using the stored `u64` timestamp (third tuple element).
3. **Add proactive epoch-boundary cleanup.** Run a periodic background task that calls `retain` to evict entries whose epoch number is older than the current epoch, rather than relying solely on the successful-block path.

## Proof of Concept
1. Spin up a victim CKB full node `V` with a known mempool state.
2. Attacker `E` connects to `V` as a P2P peer.
3. `E` mines uncle blocks (valid PoW, parent = `V`'s current tip), each including one transaction `T` not in `V`'s mempool.
4. For each mined block, `E` sends a `CompactBlock` relay message to `V`.
5. Observe in `V`'s logs: repeated `CompactBlockRequiresFreshTransactions` status codes.
6. Inspect `V`'s `pending_compact_blocks` map size via metrics or a debug endpoint — it grows by one entry per crafted block.
7. Continue for the epoch duration; monitor `V`'s RSS memory. Node crashes with OOM once memory is exhausted.
8. **Unit-level verification:** Add a test that inserts N entries into `pending_compact_blocks` without ever triggering `ReconstructionResult::Block`, and assert the map size equals N with no eviction — confirming the absence of any size cap.