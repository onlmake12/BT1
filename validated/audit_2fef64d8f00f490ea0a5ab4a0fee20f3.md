Audit Report

## Title
Global `unknown_tx_hashes` Queue Bloat via Insert-Before-Check Ordering Causes Legitimate Peer Banning - (File: `sync/src/types/mod.rs`)

## Summary
`add_ask_for_txs` in `SyncState` unconditionally inserts all attacker-supplied transaction hashes into the global `unknown_tx_hashes` queue before checking whether the queue has exceeded its size limit. An unprivileged P2P peer can exploit this ordering to bloat the queue beyond `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000), leaving garbage entries that persist for approximately 30 seconds. During this window, legitimate peers sending the maximum allowed batch of transaction hashes are incorrectly banned with `TooManyUnknownTransactions`, disrupting transaction relay.

## Finding Description

**Root cause:** In `add_ask_for_txs` (`sync/src/types/mod.rs`, lines 1486–1529), all incoming hashes are inserted unconditionally first, and the size check runs only after insertion:

```rust
// Step 1: Insert ALL hashes (up to MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767)
for tx_hash in tx_hashes.into_iter().take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER) {
    match unknown_tx_hashes.entry(tx_hash) {
        Vacant(entry) => entry.set_priority(...),  // inserted unconditionally
        ...
    }
}
// Step 2: ONLY THEN check if the queue is too large
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE   // 50000
    || unknown_tx_hashes.len() >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
{ ... }
```

**Exploit flow:**

1. Attacker connects as a P2P peer and sends a `RelayTransactionHashes` message with 32,767 unique, never-seen hashes. All 32,767 are inserted. With a single peer connected, the second condition fires immediately: `32767 >= 1 * 32767`. Per-peer count = 32,767 ≥ `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` → `TooManyUnknownTransactions` → attacker is banned. But **32,767 garbage entries remain** in the global queue.

2. The `ASK_FOR_TXS_TOKEN` timer fires every 100ms. On the first tick, `pop_ask_for_txs` processes each entry: `next_request_peer()` returns `Some(attacker_peer)` (setting `requested = true`) and pushes entries back. On subsequent ticks, `next_request_at() = original_time + RETRY_ASK_TX_TIMEOUT_INCREASE (30s)` is in the future, so entries are not popped. After 30 seconds, `next_request_peer()` returns `None` (only 1 peer, already requested) and entries are finally dropped.

3. During the ~30-second bloat window, a legitimate peer connects and sends 32,767 valid tx hashes. All are inserted (queue = 32,767 + 32,767 = 65,534). Check fires: `65534 >= 50000`. Per-peer count for the legitimate peer = 32,767 ≥ `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` → **legitimate peer receives `TooManyUnknownTransactions` and is banned**.

4. `TooManyUnknownTransactions` is status code 416 (4xx range), so `should_ban()` returns `Some(BAD_MESSAGE_BAN_TIME)` (5 minutes), and the relayer's `process()` calls `nc.ban_peer(peer, ban_time, ...)`.

5. The attacker reconnects after their 5-minute ban and repeats, maintaining persistent disruption.

**Why existing guards fail:**
- The `tx_filter` only tracks hashes of seen/verified transactions; attacker uses fresh, never-seen hashes each time.
- The rate limiter (30 req/s per peer+message-type) is not triggered — only 1 message is needed.
- The per-message cap (`MAX_RELAY_TXS_NUM_PER_BATCH = 32767`) is exactly equal to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, so a single on-spec message saturates the per-peer quota.

## Impact Explanation

This is a **High** severity finding matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

Any legitimate peer that sends the maximum allowed batch of transaction hashes while the queue is bloated is incorrectly banned for 5 minutes. An attacker can target multiple CKB nodes simultaneously with minimal resources (one TCP connection, one message per node per 5-minute cycle), causing widespread transaction relay disruption across the network. Banned legitimate peers cannot relay transactions to the victim node, degrading mempool synchronization and potentially delaying transaction confirmation network-wide.

## Likelihood Explanation

- Requires only a standard P2P connection — no privileged access, no keys, no hashpower.
- Only 1 `RelayTransactionHashes` message with 32,767 unique hashes is needed per attack cycle.
- The per-peer rate limiter (30 req/s) is not a barrier — only 1 message is required.
- The attacker can reconnect after the 5-minute ban expires and repeat indefinitely.
- The bloat window is ~30 seconds (`RETRY_ASK_TX_TIMEOUT_INCREASE`), providing ample time for legitimate peers to be affected.
- No coordination or special resources required beyond a single network connection.

## Recommendation

Move the size-limit check **before** insertion:

1. Before the insertion loop, count how many new unique hashes this peer would add (hashes not already in `unknown_tx_hashes`).
2. If the peer's current contribution already meets or exceeds `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, return `TooManyUnknownTransactions` immediately without inserting anything.
3. If the global queue is already at or above `MAX_UNKNOWN_TX_HASHES_SIZE`, return `Status::ignored()` without inserting anything.

This ensures the global queue never grows beyond the intended limit due to attacker-controlled input.

## Proof of Concept

```
1. Connect to a CKB node as a P2P peer supporting RelayV3.

2. Send RelayTransactionHashes message:
   - tx_hashes: [H1, H2, ..., H32767]  (32767 unique, non-existent tx hashes)
   - Result: all 32767 inserted into unknown_tx_hashes.
   - Size check fires: 32767 >= 1 * 32767 (with 1 peer connected).
   - Per-peer count = 32767 >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER.
   - Attacker receives TooManyUnknownTransactions ban (5 minutes).
   - Global queue size: 32767 (garbage entries remain for ~30 seconds).

3. Within ~30 seconds, connect as a legitimate peer and send:
   - RelayTransactionHashes with 32767 valid tx hashes.
   - All 32767 inserted (queue = 65534).
   - Check fires: 65534 >= 50000.
   - Per-peer count for legitimate peer = 32767 >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER.
   - Legitimate peer receives TooManyUnknownTransactions ban.

4. Repeat from step 1 after ban expires to maintain persistent disruption.
```

**Key code references:**
- Insertion loop (unconditional): `sync/src/types/mod.rs` lines 1486–1504
- Post-insertion size check: `sync/src/types/mod.rs` lines 1506–1529
- Ban enforcement: `sync/src/relayer/mod.rs` lines 195–204 (`should_ban()` on 4xx codes → `nc.ban_peer`)
- Constants: `util/constant/src/sync.rs` lines 68–72
- Timer interval (100ms): `sync/src/relayer/mod.rs` line 801
- Bloat persistence: `sync/src/types/mod.rs` lines 1268–1273 (`RETRY_ASK_TX_TIMEOUT_INCREASE = 30s`)