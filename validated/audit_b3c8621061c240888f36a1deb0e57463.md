Audit Report

## Title
Global `unknown_tx_hashes` Queue Bloat via Insert-Before-Check Ordering Causes Legitimate Peer Banning - (File: `sync/src/types/mod.rs`)

## Summary
`add_ask_for_txs` in `SyncState` unconditionally inserts all attacker-supplied transaction hashes into the global `unknown_tx_hashes` queue before evaluating the size limit. A single unprivileged P2P peer can saturate the queue with 32,767 garbage entries that persist for ~30 seconds, causing any legitimate peer that subsequently sends the maximum allowed batch of transaction hashes to be incorrectly banned with `TooManyUnknownTransactions` (status 416 → 5-minute ban), disrupting transaction relay.

## Finding Description

**Root cause:** In `add_ask_for_txs` (`sync/src/types/mod.rs` lines 1486–1532), all incoming hashes are inserted unconditionally before the size check runs:

```rust
// Lines 1486–1504: unconditional insertion
for tx_hash in tx_hashes.into_iter().take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER) {
    match unknown_tx_hashes.entry(tx_hash) {
        Vacant(entry) => entry.set_priority(UnknownTxHashPriority { ... }),
        Occupied(entry) => { ... push_peer ... }
    }
}

// Lines 1506–1529: size check runs AFTER insertion
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE          // 50000
    || unknown_tx_hashes.len()
        >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER  // N * 32767
{
    // count per-peer entries, ban if >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
}
```

**Constants** (`util/constant/src/sync.rs` lines 68–72):
- `MAX_RELAY_TXS_NUM_PER_BATCH = 32767`
- `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = MAX_RELAY_TXS_NUM_PER_BATCH = 32767`
- `MAX_UNKNOWN_TX_HASHES_SIZE = 50000`
- `RETRY_ASK_TX_TIMEOUT_INCREASE = 30s`

**Exploit flow:**

1. Attacker connects as a P2P peer and sends one `RelayTransactionHashes` message with 32,767 unique, never-seen hashes. All 32,767 are inserted. With 1 peer connected: `32767 >= 1 * 32767` → size check fires. `peer_unknown_counter = 32767 >= 32767` → `StatusCode::TooManyUnknownTransactions` returned. `should_ban()` returns `Some(BAD_MESSAGE_BAN_TIME)` (5 min) because code 416 is in the 4xx range (`sync/src/status.rs` lines 165–179). Attacker is banned and disconnected. **32,767 garbage entries remain in the global queue.**

2. `ASK_FOR_TXS_TOKEN` fires every 100ms (`sync/src/relayer/mod.rs` line 801). On the first tick, `pop_ask_for_txs` pops each entry: `should_request(now)` is true (not yet requested), `next_request_peer()` sets `requested = true` and returns `Some(attacker_peer)`, entry is pushed back. On subsequent ticks within 30s: `next_request_at() = request_time + RETRY_ASK_TX_TIMEOUT_INCREASE` is in the future → `should_request` is false → entries are pushed back and the loop breaks. After 30s: `should_request` is true again, `next_request_peer()` returns `None` (only 1 peer, already requested, `peers.len() > 1` is false, `sync/src/types/mod.rs` lines 1276–1289) → entries are dropped (not pushed back, lines 1466–1479). **Bloat window: ~30 seconds.**

3. During the bloat window, a legitimate peer connects (attacker is gone, so `peers.state.len() = 1`). Legitimate peer sends 32,767 valid tx hashes. All are inserted: queue = 65,534. Check fires: `65534 >= 50000` → TRUE. `peer_unknown_counter` for legitimate peer = 32,767 ≥ 32,767 → `TooManyUnknownTransactions` → **legitimate peer is banned for 5 minutes**.

4. Attacker reconnects after ban expires and repeats, maintaining persistent disruption.

**Why existing guards fail:**
- `tx_filter` only tracks hashes of seen/verified transactions; attacker uses fresh, never-seen hashes each time.
- Rate limiter (30 req/s per peer+message-type) is not triggered — only 1 message is needed per cycle.
- `MAX_RELAY_TXS_NUM_PER_BATCH` equals `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` exactly, so a single on-spec message saturates the per-peer quota.

## Impact Explanation

This is a **High** severity finding matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

Any legitimate peer sending the maximum allowed batch of transaction hashes while the queue is bloated is incorrectly banned for 5 minutes. An attacker can target multiple CKB nodes simultaneously with minimal resources (one TCP connection, one message per node per 5-minute cycle), causing widespread transaction relay disruption. Banned legitimate peers cannot relay transactions to the victim node, degrading mempool synchronization and potentially delaying transaction confirmation network-wide.

## Likelihood Explanation

- Requires only a standard P2P connection — no privileged access, no keys, no hashpower.
- Only 1 `RelayTransactionHashes` message with 32,767 unique hashes is needed per attack cycle.
- The per-peer rate limiter is not a barrier — only 1 message is required.
- The attacker reconnects after the 5-minute ban and repeats indefinitely.
- The bloat window is ~30 seconds, providing ample time for legitimate peers to be affected.
- No coordination or special resources required.

## Recommendation

Move the size-limit check **before** insertion:

1. Before the insertion loop, count how many new unique hashes this peer would add (hashes not already in `unknown_tx_hashes`).
2. If the peer's projected contribution meets or exceeds `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, return `TooManyUnknownTransactions` immediately without inserting anything.
3. If the global queue is already at or above `MAX_UNKNOWN_TX_HASHES_SIZE`, return `Status::ignored()` without inserting anything.

This ensures the global queue never grows beyond the intended limit due to attacker-controlled input.

## Proof of Concept

```
1. Connect to a CKB node as a P2P peer supporting RelayV3.

2. Send RelayTransactionHashes message:
   - tx_hashes: [H1, H2, ..., H32767]  (32767 unique, non-existent tx hashes)
   - Result: all 32767 inserted into unknown_tx_hashes.
   - Size check fires: 32767 >= 1 * 32767 (with 1 peer connected).
   - peer_unknown_counter = 32767 >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER.
   - Attacker receives TooManyUnknownTransactions ban (5 minutes).
   - Global queue size: 32767 (garbage entries remain for ~30 seconds).

3. Within ~30 seconds, connect as a legitimate peer and send:
   - RelayTransactionHashes with 32767 valid tx hashes.
   - All 32767 inserted (queue = 65534).
   - Check fires: 65534 >= 50000.
   - peer_unknown_counter for legitimate peer = 32767 >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER.
   - Legitimate peer receives TooManyUnknownTransactions ban (5 minutes).

4. Repeat from step 1 after ban expires to maintain persistent disruption.

Existing integration test (test/src/specs/relay/too_many_unknown_transactions.rs)
confirms the ban path is reachable with exactly MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
hashes in a single message.
```