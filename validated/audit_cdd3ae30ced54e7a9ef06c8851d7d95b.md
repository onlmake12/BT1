Audit Report

## Title
Insert-Before-Check Ordering in `add_ask_for_txs` Enables `unknown_tx_hashes` Queue Exhaustion DoS - (`sync/src/types/mod.rs`)

## Summary

`SyncState::add_ask_for_txs` unconditionally inserts all hashes from a peer into the global `unknown_tx_hashes` queue before checking whether the global or per-peer limits are exceeded. An attacker with two P2P connections can fill the 50,000-entry global queue with fake hashes, causing all subsequent `RelayTransactionHashes` messages from legitimate peers to return `Status::ignored()` until the fake entries naturally drain (~30 seconds per cycle). The attacker can sustain the disruption continuously by reconnecting from new IPs, causing recurring transaction relay failure on the victim node.

## Finding Description

In `sync/src/types/mod.rs`, `add_ask_for_txs` (lines 1483–1532) performs insertion first, then checks limits:

```rust
// Lines 1486–1504: ALL hashes inserted unconditionally, up to 32,767
for tx_hash in tx_hashes.into_iter().take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER) {
    match unknown_tx_hashes.entry(tx_hash) {
        Vacant(entry) => { entry.set_priority(...) }  // inserted here
        ...
    }
}

// Lines 1507–1528: limit check AFTER insertion
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE { // 50,000
    ...
    if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
        return StatusCode::TooManyUnknownTransactions.into(); // ban
    }
    return Status::ignored(); // legitimate peers silently dropped
}
```

The constants (`util/constant/src/sync.rs` lines 68–72) are: `MAX_RELAY_TXS_NUM_PER_BATCH = MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32,767` and `MAX_UNKNOWN_TX_HASHES_SIZE = 50,000`. Two attacker connections (32,767 + 17,233 unique fake hashes) fill the queue to capacity.

Once full, any legitimate peer whose per-peer count is below 32,767 hits `Status::ignored()` — no ban, no error, silent drop. The victim node stops fetching those transactions.

Regarding persistence: the report's claim that entries "never expire" is partially overstated. `pop_ask_for_txs` (lines 1466–1479) drops entries when `next_request_peer()` returns `None` — which occurs after the first retry cycle (~30 seconds via `RETRY_ASK_TX_TIMEOUT_INCREASE`) when the announcing peer is banned/disconnected. However, this does not invalidate the vulnerability: the attacker can continuously reconnect from new IPs every ~30 seconds to re-fill the queue before it drains, sustaining the DoS with trivial cost. The `BAD_MESSAGE_BAN_TIME` is only 5 minutes per IP (`util/constant/src/sync.rs` lines 59–62), and the attacker only needs to rotate IPs.

The only removal path for entries is `mark_as_known_txs` (lines 1443–1451), which requires the actual transaction to be received and verified — impossible for fake hashes — or the natural drain via `pop_ask_for_txs` after the requesting peer is gone.

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. The victim node's transaction relay is disrupted: it stops fetching new unconfirmed transactions from all peers while the queue is saturated. Miners on the victim node miss fee-paying transactions; users whose transactions propagate only through the victim's peers see delayed or missed confirmations. The attack targets a single node (not the whole network), which places it at High rather than Critical.

## Likelihood Explanation

Any unauthenticated P2P peer can send `RelayTransactionHashes` messages. The protocol-level check in `TransactionHashesProcess::execute` (lines 29–35) only rejects messages exceeding `MAX_RELAY_TXS_NUM_PER_BATCH` — it does not prevent queue exhaustion. The total payload is ~2 MB (50,000 × 32 bytes). The attacker needs only two connections and ~2 MB of data to saturate the queue, then ~32 KB every ~30 seconds from a new IP to maintain it. IP rotation is trivially achievable via VPNs, Tor, or a botnet. The ban time of 5 minutes per IP is not a meaningful deterrent.

## Recommendation

1. **Check limits before inserting**: At the top of `add_ask_for_txs`, before the insertion loop, check whether the global queue is already at `MAX_UNKNOWN_TX_HASHES_SIZE` or whether the per-peer count already reaches `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`. Reject or truncate the input before any insertion occurs.

2. **Evict entries from banned/disconnected peers**: When a peer is banned or disconnects, remove all `unknown_tx_hashes` entries whose `peers` list contains only that peer. This prevents fake hashes from occupying queue space even during the ~30-second drain window.

3. **Add TTL to `unknown_tx_hashes`**: Unlike `tx_filter` (which uses `TtlFilter`), `unknown_tx_hashes` has no time-based expiry. Adding a bounded TTL (e.g., 60 seconds) would ensure fake entries are evicted promptly regardless of `pop_ask_for_txs` scheduling.

## Proof of Concept

1. Attacker connects to victim as peer A. Sends one `RelayTransactionHashes` message with 32,767 unique non-existent tx hashes.
   - `add_ask_for_txs` inserts all 32,767 hashes into `unknown_tx_hashes`.
   - Post-insertion check: `peer_unknown_counter = 32,767 >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` → peer A is banned. Fake hashes remain.
2. Attacker reconnects as peer B (new IP). Sends 17,233 more unique fake hashes.
   - `unknown_tx_hashes.len()` reaches 50,000. Peer B's per-peer count is 17,233 < 32,767 → `Status::ignored()` (no ban yet, hashes inserted).
3. Global queue is now at capacity (50,000 fake entries).
4. Legitimate peer C sends a `RelayTransactionHashes` message with real tx hashes.
   - `add_ask_for_txs` inserts them, then checks: `unknown_tx_hashes.len() >= 50,000` → true. Peer C's per-peer count is small → `return Status::ignored()`. Victim never requests those transactions.
5. After ~30 seconds, `pop_ask_for_txs` drains the fake entries (banned peers return `None` from `next_request_peer()`).
6. Attacker reconnects from a new IP and repeats step 1 to re-saturate the queue before legitimate traffic recovers.

The existing integration test `test/src/specs/relay/too_many_unknown_transactions.rs` confirms the ban path for a single peer exceeding the per-peer limit, but does not test the cross-peer queue exhaustion scenario described here.