Audit Report

## Title
Peer Entry in `pending_compact_blocks` Not Removed on `BlockTransactions` Verification Failure — (`sync/src/relayer/block_transactions_process.rs`)

## Summary
In `BlockTransactionsProcess::execute()`, when `BlockTransactionsVerifier::verify()` or `BlockUnclesVerifier::verify()` fails via the `attempt!` macro, the function returns early without removing the peer's entry from `pending_compact_blocks`. The peer entry remains stale, locking the peer out of re-initiating compact block reconstruction for that block hash via `CompactBlockIsAlreadyPending`. An attacker can repeat this across many block hashes within an epoch to accumulate stale entries and degrade block propagation.

## Finding Description
In `block_transactions_process.rs`, the execution flow enters nested `Entry::Occupied` guards at lines 65–72. At lines 80–89, two `attempt!` calls perform structural verification:

```rust
attempt!(BlockTransactionsVerifier::verify(
    compact_block,
    expected_transaction_indexes,
    &received_transactions,
));
attempt!(BlockUnclesVerifier::verify(
    compact_block,
    expected_uncle_indexes,
    &received_uncles,
));
```

The `attempt!` macro returns early on failure. At this point, neither `value.remove()` nor any equivalent cleanup is called — the `OccupiedEntry` for the peer is simply dropped, leaving the peer's `(expected_transaction_indexes, expected_uncle_indexes)` tuple intact in `peers_map`. The same absence of cleanup applies to the `ReconstructionResult::Error` arm at lines 160–162, which also returns without touching the peer entry.

The only path that removes the entry is `ReconstructionResult::Block` at line 122 (`pending.remove()`), which is unreachable when verification fails.

Subsequently, `contextual_check` in `compact_block_process.rs` at lines 284–291 detects the stale peer entry and returns `CompactBlockIsAlreadyPending`, preventing the peer from re-initiating reconstruction:

```rust
if pending_compact_blocks
    .get(&block_hash)
    .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
    .unwrap_or(false)
{
    return StatusCode::CompactBlockIsAlreadyPending.with_context(block_hash);
}
```

Additionally, `SyncState::disconnected()` at lines 1607–1616 of `types/mod.rs` removes inflight blocks and peer state but never iterates `pending_compact_blocks`, so stale peer entries survive peer disconnection.

The existing epoch-based cleanup in `compact_block_process.rs` (lines 112–116) only fires when a block is successfully reconstructed from a compact block, and only removes entries from epochs older than the accepted block — it does not address stale per-peer entries within the current epoch.

## Impact Explanation
An unprivileged remote peer can lock itself into a permanently pending state for any block hash by sending a valid compact block followed by a structurally invalid `BlockTransactions` response. Repeating this across many block hashes within an epoch accumulates stale entries in `pending_compact_blocks`, increasing memory consumption proportional to the number of targeted block hashes. Affected block hashes fall back to full-block sync, increasing bandwidth and latency for block propagation. With multiple attacker-controlled connections, this can cause sustained CKB network congestion with minimal cost to the attacker. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
Any outbound or inbound peer can trigger this without special privileges, keys, or hashpower. The attacker controls both the compact block message and the `BlockTransactions` response. The condition is reachable on mainnet from any connected peer. The attack is repeatable across block hashes and peer connections, and the cost per stale entry is a single round-trip of two messages.

## Recommendation
In `BlockTransactionsProcess::execute()`, on every early-return path — both `attempt!` failures and `ReconstructionResult::Error` — call `value.remove()` to remove the peer's entry from `peers_map`. If `peers_map` becomes empty after removal, call `pending.remove()` to remove the entire block hash entry. Additionally, `SyncState::disconnected()` should acquire the `pending_compact_blocks` lock and remove all per-peer entries for the disconnecting peer index, removing the block hash entry entirely if its `peers_map` becomes empty.

## Proof of Concept
1. Connect to a CKB node as peer P.
2. Observe or mine a block B with at least one non-prefilled transaction.
3. Send a valid `CompactBlock` message for B with one short ID → node inserts `(P, ([tx_index], []))` into `pending_compact_blocks` and sends `GetBlockTransactions`.
4. Respond with a `BlockTransactions` message where `transactions.len() != expected_transaction_indexes.len()` → `BlockTransactionsVerifier::verify` fails, `attempt!` returns `BlockTransactionsLengthIsUnmatchedWithPendingCompactBlock`, peer entry is NOT removed (confirmed by `test_missing_requested_uncle_response_is_rejected` in `sync/src/relayer/tests/block_transactions_process.rs` which shows the verifier fires but does not assert cleanup).
5. Send the same `CompactBlock` again → node returns `CompactBlockIsAlreadyPending`.
6. Repeat steps 3–5 for different block hashes within the same epoch to grow `pending_compact_blocks` without epoch-based eviction firing.