Audit Report

## Title
Global `unknown_tx_hashes` Soft Limit Bypassed by Post-Insertion Check in `add_ask_for_txs` - (File: sync/src/types/mod.rs)

## Summary
`SyncState::add_ask_for_txs` unconditionally inserts up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32,767) entries per peer into the shared `unknown_tx_hashes` `KeyedPriorityQueue` before checking the global soft limit (`MAX_UNKNOWN_TX_HASHES_SIZE` = 50,000). With just two peers each sending disjoint sets of 32,767 novel tx hashes, the map grows to 65,534 entries — 31% above the intended cap — with no effective global enforcement. Once the map is oversized, every subsequent call triggers an O(map_size) full scan while holding the mutex, blocking `pop_ask_for_txs` and `mark_as_known_txs` and degrading transaction relay on the node.

## Finding Description

**Code path:** `TransactionHashesProcess::execute` (sync/src/relayer/transaction_hashes_process.rs:25–50) → `SyncState::add_ask_for_txs` (sync/src/types/mod.rs:1483–1532).

**Root cause:** The insertion loop at lines 1486–1504 runs unconditionally for up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32,767) hashes before the global-limit check at lines 1507–1529. The check is post-insertion and per-peer:

```rust
// Lines 1486–1504: insert first
for tx_hash in tx_hashes.into_iter().take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER) {
    match unknown_tx_hashes.entry(tx_hash) { ... }
}

// Lines 1507–1509: check after
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
    || unknown_tx_hashes.len() >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
{
    // Lines 1516–1526: O(map_size) scan, only rejects if THIS peer >= 32,767
    ...
    if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
        return StatusCode::TooManyUnknownTransactions.into();
    }
    return Status::ignored(); // entries already inserted, remain in map
}
```

**Constants confirm the mismatch** (util/constant/src/sync.rs:68–72):
- `MAX_RELAY_TXS_NUM_PER_BATCH` = 32,767
- `MAX_UNKNOWN_TX_HASHES_SIZE` = 50,000
- `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` = 32,767

Two peers × 32,767 = 65,534 > 50,000. The global cap is structurally smaller than 2× the per-peer quota.

**Why existing guards fail:**
- The rate limiter in `Relayer::try_process` (sync/src/relayer/mod.rs:116–123) limits message frequency per peer (30 req/s) but does not bound cumulative map size across peers.
- `TransactionHashesProcess::execute` filters only hashes already in `tx_filter` (known transactions); novel fake hashes pass through freely.
- The second condition `peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` grows with peer count, providing no tighter bound than the 50,000 cap for small N.
- When the threshold fires and the current peer has < 32,767 entries, `Status::ignored()` is returned but all inserted entries remain in the map.

**O(map_size) mutex contention:** Once the map is oversized, every call to `add_ask_for_txs` from any peer triggers the full-map scan (lines 1517–1523) while holding `self.unknown_tx_hashes.lock()`. This directly blocks `pop_ask_for_txs` (lines 1453–1481, same mutex, called every 100ms via `ASK_FOR_TXS_TOKEN`) and `mark_as_known_txs` (lines 1443–1451, same mutex).

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

With 2 peers, the global map exceeds its intended cap (65,534 vs 50,000). With N peers, it grows to N × 32,767 with no effective ceiling. Each `UnknownTxHashPriority` entry holds an `Instant` + `Vec<PeerIndex>`, so memory grows linearly with N. More critically, once oversized, every subsequent `RelayTransactionHashes` message from any peer triggers an O(map_size) scan under the mutex, blocking the relay timer (`ASK_FOR_TXS_TOKEN`, 100ms interval) that dispatches `GetRelayTransactions` to peers. This stalls transaction propagation on the targeted node. An attacker with a modest number of connections (2–10 peers) can sustain this degraded state indefinitely by continuously sending disjoint fake tx hashes, causing the node to fall behind in transaction relay and contributing to network-wide propagation delays.

## Likelihood Explanation

**Medium.** Any peer that completes the P2P handshake can send `RelayTransactionHashes`. The hashes need not correspond to real transactions — only hashes already in `tx_filter` are filtered out, and novel fake hashes pass through. An attacker needs only 2 connections to exceed the 50,000 global limit. The per-peer rate limiter (30 req/s) limits how quickly a single peer can refill the map after `pop_ask_for_txs` drains it, but with 2+ peers operating concurrently, the map stays oversized. No special privilege, key, or hashpower is required.

## Recommendation

Check the global limit **before** the insertion loop, not after. If `unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE`, perform the per-peer count check first and reject or skip insertion accordingly. Additionally, maintain a separate `HashMap<PeerIndex, usize>` tracking per-peer entry counts to replace the O(map_size) scan with an O(1) lookup on every overflow check.

## Proof of Concept

1. Connect two peers (Peer A, Peer B) to a CKB node after handshake.
2. Peer A sends one `RelayTransactionHashes` message with 32,767 unique, novel tx hashes (not in `tx_filter`). All are inserted; map size = 32,767 < 50,000 → no limit check triggered.
3. Peer B sends one `RelayTransactionHashes` message with 32,767 **different** unique tx hashes. All are inserted; map size = 65,534. Post-insertion check fires (65,534 ≥ 50,000). Peer B's per-peer count = 32,767 ≥ `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` → `TooManyUnknownTransactions` returned, but all 32,767 entries are already in the map.
4. Observe `unknown_tx_hashes.len()` = 65,534, 31% above the intended 50,000 cap.
5. Now send any subsequent `RelayTransactionHashes` from any peer: the post-insertion check fires immediately, triggering the O(65,534) full-map scan under the mutex. Measure mutex hold time and observe `pop_ask_for_txs` (100ms timer) being blocked.
6. Add a third peer (Peer C) with 32,767 more hashes: map grows to 98,301 before the check fires; Peer C has < 32,767 entries → `Status::ignored()`, entries remain. Repeat to grow the map further.