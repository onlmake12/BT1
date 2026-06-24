Audit Report

## Title
Unguarded `.expect("parent header stored")` in Preload Thread Causes Permanent Node Halt via Race with Verify Thread Deleting Parent Block — (File: chain/src/preload_unverified_blocks_channel.rs)

## Summary
The preload thread calls `.expect("parent header stored")` when loading a child block's parent header, but the verify thread can concurrently delete that parent's header from `COLUMN_BLOCK_HEADER` upon contextual verification failure. Because the preload thread has no `catch_unwind` protection, the resulting panic kills the thread, drops the `unverified_block_tx` sender, and causes the verify thread to exit on the next `Err` from `unverified_block_rx` — permanently halting all block verification on the node.

## Finding Description
The three-stage pipeline is: orphan broker → preload channel → verify channel.

**Root cause:** In `load_full_unverified_block_by_hash` (`preload_unverified_blocks_channel.rs:91-96`), the preload thread calls `store().get_block_header(&parent_hash).expect("parent header stored")` with no fallback. This assumption is violated when the verify thread concurrently deletes the parent block.

**Race path:**
1. Parent block P arrives; `orphan_broker.rs:200-201` inserts P into `is_pending_verify` and sends P to the preload channel.
2. Child block C (parent = P) arrives; `orphan_broker.rs:111-118` sees P in `is_pending_verify` and sends C to the preload channel as well. Both P and C now sit in the bounded preload channel.
3. Preload thread dequeues P, loads it, forwards it to the verify channel.
4. Verify thread processes P; contextual verification fails; `verify.rs:173` calls `self.delete_unverified_block(&block)` → `lib.rs:204` → `StoreTransaction::delete_block` (`transaction.rs:216`) deletes `COLUMN_BLOCK_HEADER` for P's hash. `is_pending_verify.remove(&P.hash())` follows at `verify.rs:193`.
5. Preload thread dequeues C, calls `get_block_header(&P.hash())` → `None` → `.expect()` panics.
6. No `catch_unwind` exists in `PreloadUnverifiedBlocksChannel::start()` (`preload_unverified_blocks_channel.rs:33-51`), unlike the verify thread which wraps `consume_unverified_blocks` in `catch_unwind` at `verify.rs:86-96`.
7. The preload thread dies; `unverified_block_tx` is dropped; the verify thread's `unverified_block_rx` returns `Err` at `verify.rs:103-106`, causing it to `return` — permanently halting block verification.

**Existing guards are insufficient:** The `BLOCK_INVALID` status check in `orphan_broker.rs:42-49` only fires for blocks still in the orphan pool; C has already been dequeued and is in the preload channel before P is marked invalid. The `catch_unwind` in the verify thread (`verify.rs:86`) does not protect the preload thread.

## Impact Explanation
Permanent halt of all block verification on the targeted node. The node process continues running but cannot advance its chain tip, rendering it useless as a full node. This matches the allowed CKB bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation
The attacker must mine one block P with valid PoW but invalid contextual content (e.g., a transaction spending a non-existent cell or a double-spend). This requires hash power to produce one valid-PoW block — a meaningful but not majority-hashpower barrier. Once mined, P and its child C can be broadcast to the target node via standard P2P sync. The race window is wide: the preload channel is bounded at `BLOCK_DOWNLOAD_WINDOW * 10` items, giving the verify thread ample time to delete P's header before the preload thread dequeues C. The attack is repeatable and deterministic once the attacker controls the block content.

## Recommendation
1. Replace the `.expect()` in `load_full_unverified_block_by_hash` with graceful error handling: if `get_block_header` returns `None`, log an error, invoke the block's error callback (if any), and skip forwarding to the verify channel.
2. Add `catch_unwind` around the body of `preload_unverified_channel` in `PreloadUnverifiedBlocksChannel::start()`, mirroring the protection already present in the verify thread (`verify.rs:86-96`).

## Proof of Concept
1. Mine block P: valid PoW, invalid transaction (e.g., spends a non-existent cell output).
2. Send P to the target node via P2P; P passes non-contextual validation, is stored in DB, and enters `is_pending_verify`.
3. Immediately send child block C (parent hash = P's hash) to the target node; C is enqueued in the preload channel because P is in `is_pending_verify` (`orphan_broker.rs:111-118`).
4. The verify thread dequeues P from the verify channel, fails contextual verification, calls `delete_unverified_block(P)` → `COLUMN_BLOCK_HEADER` for P is deleted.
5. The preload thread dequeues C, calls `store().get_block_header(&P.hash()).expect("parent header stored")` → `None` → panic → preload thread exits.
6. `unverified_block_tx` is dropped; verify thread's `unverified_block_rx` returns `Err` → verify thread exits → node halts block verification permanently.
7. Confirm: node's chain tip stops advancing; no new blocks are processed.