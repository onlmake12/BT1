Audit Report

## Title
Post-Insertion Limit Check in `add_ask_for_txs` Allows Unprivileged Peer to Exhaust `unknown_tx_hashes` Queue and Trigger O(n) Mutex-Held Scan - (File: `sync/src/types/mod.rs`)

## Summary

`SyncState::add_ask_for_txs` unconditionally inserts up to 32,767 hashes into the shared `unknown_tx_hashes` queue before checking the global soft-cap of 50,000. When the cap is exceeded, the function performs an O(n) linear scan of the entire queue while holding the mutex. Because inserted hashes are never cleaned up on peer ban or disconnect, an attacker can permanently saturate the queue in two messages, causing every subsequent legitimate peer that announces a full batch to be immediately banned — disrupting transaction propagation across the node.

## Finding Description

In `sync/src/types/mod.rs`, `add_ask_for_txs` (L1483–1532) is called from `TransactionHashesProcess::execute` (L25–50 of `sync/src/relayer/transaction_hashes_process.rs`) for every `RelayTransactionHashes` P2P message.

**Root cause 1 — Insert-before-check:** Lines 1486–1504 unconditionally insert all hashes (up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` = 32,767) into the queue. The global-cap check at lines 1507–1529 fires only after insertion, allowing the queue to grow well beyond `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000) before any enforcement.

**Root cause 2 — O(n) scan under mutex:** When the post-insertion check fires (L1507), lines 1516–1523 iterate over every entry in the queue counting how many belong to the current peer. This entire scan holds the `unknown_tx_hashes` mutex, blocking `pop_ask_for_txs` (L1453) and `mark_as_known_txs` (L1443) for the duration.

**Root cause 3 — No cleanup on ban/disconnect:** `TooManyUnknownTransactions` (StatusCode 416) is in the 4xx range, so `should_ban()` in `sync/src/status.rs` (L165–179) returns `Some(BAD_MESSAGE_BAN_TIME)` = 5 minutes. However, no code path removes the attacker's entries from `unknown_tx_hashes` upon ban or peer disconnect. The queue remains saturated indefinitely.

**Root cause 4 — Rate limiter insufficient:** The Relayer's token-bucket rate limiter (30 req/sec per `(PeerIndex, message_type)`, `sync/src/relayer/mod.rs` L88–92) permits at least 2 messages before the queue is full. Two messages of 32,767 hashes each saturate the 50,000-entry cap within one second.

**Collateral banning of legitimate peers:** After the attacker's 65,534 entries remain in the queue, any legitimate peer sending a full batch of 32,767 hashes will have those hashes inserted (queue = 98,301), trigger the O(n) scan, be found to have exactly 32,767 entries (≥ `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`), and be banned via `StatusCode::TooManyUnknownTransactions`.

## Impact Explanation

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

Concretely:
- Transaction propagation is disrupted: the node cannot track unknown transactions from honest peers because every peer announcing a full batch is immediately banned.
- Honest peers are banned, degrading the node's P2P connectivity.
- The O(n) mutex-held scan causes CPU spikes and blocks `pop_ask_for_txs` and `mark_as_known_txs` on every attack message, stalling the transaction relay pipeline.
- Memory grows beyond the intended 50,000-entry cap before any enforcement action.

## Likelihood Explanation

- **Fully unprivileged:** Any peer completing the standard P2P handshake can send `RelayTransactionHashes`. No stake, key, or special role required.
- **Trivial cost:** Two messages of 32,767 × 32-byte hashes ≈ 2 MB total, sent within one second.
- **Rate limiter does not prevent saturation:** 30 req/sec allows both messages before the queue fills.
- **No automatic recovery:** Entries have no TTL and are not removed on peer disconnect or ban. The queue stays saturated until legitimate transactions happen to match the garbage hashes (negligible probability with random hashes).
- **Repeatable:** After the 5-minute ban expires, the attacker reconnects and re-saturates in 2 messages.

## Recommendation

1. **Check before inserting:** Query the per-peer count in `unknown_tx_hashes` before inserting any hashes. If the peer is already at or near `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, reject the message immediately without touching the queue.
2. **Maintain a per-peer counter:** Store a `HashMap<PeerIndex, usize>` alongside `unknown_tx_hashes` so the per-peer count can be read in O(1) instead of O(n). Update it atomically with insertions and removals.
3. **Evict on peer disconnect/ban:** When a peer is banned or disconnects, remove its entries from `unknown_tx_hashes` to prevent the queue from staying saturated after the attacker is gone.
4. **Add a TTL to queue entries:** Entries older than a configurable timeout (e.g., 2× `RETRY_ASK_TX_TIMEOUT_INCREASE` = 60 seconds) should be expired, preventing indefinite saturation by non-responsive peers.

## Proof of Concept

```
1. Connect an unprivileged peer to the CKB node (standard P2P handshake).

2. Send RelayTransactionHashes message #1:
   - tx_hashes: [H1, H2, ..., H32767]  (32,767 random 32-byte hashes)
   - add_ask_for_txs inserts all 32,767 hashes (L1486-1504).
   - Post-insertion check: 32,767 < 50,000 → Status::ok(). Peer NOT banned.

3. Send RelayTransactionHashes message #2 (within the same second):
   - tx_hashes: [H32768, ..., H65534]  (32,767 more random hashes)
   - add_ask_for_txs inserts all 32,767 hashes → queue = 65,534.
   - Post-insertion check fires (L1507): 65,534 >= 50,000.
   - O(65,534-entry) scan (L1516-1523): peer has 65,534 entries >= 32,767.
   - Returns StatusCode::TooManyUnknownTransactions → peer banned 5 minutes.
   - Queue remains at 65,534 entries (no cleanup).

4. Connect a legitimate peer and send RelayTransactionHashes with 32,767 hashes:
   - All 32,767 hashes inserted → queue = 98,301.
   - O(n) scan: legitimate peer has 32,767 entries >= 32,767 → legitimate peer banned.

5. Repeat step 4 for every new peer: each is banned immediately upon announcing transactions.

6. After BAD_MESSAGE_BAN_TIME (5 minutes), reconnect the malicious peer and repeat
   from step 2 to keep the queue saturated indefinitely.
```