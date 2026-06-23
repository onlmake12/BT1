### Title
Global `unknown_tx_hashes` Soft Limit Bypassed by Per-Peer Insertion Before Check - (`File: sync/src/types/mod.rs`)

### Summary

`SyncState::add_ask_for_txs` inserts up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32,767) entries from each peer into the global `unknown_tx_hashes` map **before** checking the global soft limit (`MAX_UNKNOWN_TX_HASHES_SIZE` = 50,000). Because the limit check is post-insertion and per-peer, N concurrent peers can each fill their per-peer quota, growing the global map to N × 32,767 entries — far beyond the intended 50,000 cap. This is the direct CKB analog of the `LevelMintingV2` per-user vs. global limit bug.

### Finding Description

In `sync/src/types/mod.rs`, `add_ask_for_txs` is called from `TransactionHashesProcess::execute` whenever a peer sends a `RelayTransactionHashes` P2P message. The function:

1. **Unconditionally inserts** up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32,767) new tx-hash entries from the calling peer into the shared `unknown_tx_hashes` `KeyedPriorityQueue` (lines 1486–1504).
2. **Only after insertion**, checks whether the global map length has exceeded `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000) or `peers.len() × MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (lines 1507–1509).
3. When the global threshold is exceeded, it counts how many entries the **current peer** contributed (lines 1516–1523) and only returns `TooManyUnknownTransactions` if that peer's count ≥ 32,767 (line 1524). Otherwise it returns `Status::ignored()` — but the entries are already in the map.

The root cause is identical to the external report: the limit is enforced per-entity (per-peer) rather than globally, and the check happens after the data is already inserted.

**Concrete scenario:**
- `MAX_UNKNOWN_TX_HASHES_SIZE` = 50,000; `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` = 32,767
- Peer A sends 32,767 unique tx hashes → all inserted (map size: 32,767)
- Peer B sends 32,767 different unique tx hashes → all inserted (map size: 65,534 — 31% over the global limit)
- Peer C sends 32,767 more → inserted first (map: 98,301), then the post-insertion check fires; since Peer C has < 32,767 entries, it returns `Status::ignored()` — but the entries remain
- With N peers: map grows to N × 32,767 entries with no effective global cap [1](#0-0) [2](#0-1) 

The per-peer rate limiter in `Relayer::try_process` (30 req/s per peer per message type) does not prevent this — it limits message frequency, not the cumulative map size across peers. [3](#0-2) 

The entry point is `TransactionHashesProcess::execute`, which accepts up to `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) hashes per message and passes them directly to `add_ask_for_txs`. [4](#0-3) 

The constants confirm the mismatch: the global cap (50,000) is smaller than 2 × per-peer quota (2 × 32,767 = 65,534). [5](#0-4) 

### Impact Explanation

The `unknown_tx_hashes` map is a `Mutex`-protected `KeyedPriorityQueue`. Exceeding the intended size causes:

1. **Memory growth**: Each entry holds a `Byte32` hash plus a `Vec<PeerIndex>`. With N peers each inserting 32,767 entries, memory grows linearly with N.
2. **Mutex contention and CPU spike**: When the global threshold is exceeded, the per-peer counting loop (lines 1516–1523) iterates the **entire** map while holding the mutex lock. This is O(map_size × avg_peers_per_entry). With a large map, this blocks `pop_ask_for_txs` and `mark_as_known_txs`, stalling the relay subsystem's transaction propagation.
3. **Relay degradation**: The relay timer that calls `pop_ask_for_txs` to dispatch tx requests to peers is blocked during the O(N) scan, delaying transaction propagation across the node. [6](#0-5) 

### Likelihood Explanation

**Medium.** Any unprivileged peer that completes the P2P handshake can send `RelayTransactionHashes` messages. The message format allows up to 32,767 hashes per message. An attacker needs only 2 peers (or 2 connections from the same attacker) sending disjoint sets of fake tx hashes to exceed the 50,000 global limit. The hashes do not need to correspond to real transactions — the filter only removes hashes already in `tx_filter` (known transactions), so novel fake hashes pass through. No special privilege, key, or majority hashpower is required. [7](#0-6) 

### Recommendation

Check the global limit **before** inserting, not after. Specifically, in `add_ask_for_txs`, check whether the map is already at or above `MAX_UNKNOWN_TX_HASHES_SIZE` before the insertion loop. If the global limit is reached, apply the per-peer check first and reject or drop accordingly — mirroring the approach used in minting (the external report's recommendation). Additionally, consider tracking per-peer entry counts in a separate `HashMap<PeerIndex, usize>` to avoid the O(map_size) scan on every overflow check.

### Proof of Concept

1. Connect two peers (Peer A, Peer B) to a CKB node.
2. Peer A sends a `RelayTransactionHashes` message containing 32,767 unique, novel tx hashes (not in `tx_filter`). These are inserted into `unknown_tx_hashes` (map size: 32,767 < 50,000 → no limit check triggered).
3. Peer B sends a `RelayTransactionHashes` message containing 32,767 **different** unique tx hashes. These are inserted (map size: 65,534). The post-insertion check fires (65,534 ≥ 50,000), but Peer B's per-peer count is 32,767 — exactly at the threshold — so it returns `TooManyUnknownTransactions` only if `>= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`. Since Peer B just inserted exactly 32,767, it hits the boundary. A third peer (Peer C) with 32,767 more hashes would be inserted first (map: 98,301), then the check fires, Peer C has < 32,767 entries → `Status::ignored()`, entries remain.
4. Observe `unknown_tx_hashes.len()` = 65,534–98,301, well above the intended 50,000 cap.
5. Observe that the per-peer counting loop (O(map_size)) now runs on every subsequent call from any peer while the map is oversized, causing mutex hold time to spike and blocking `pop_ask_for_txs`. [8](#0-7) [5](#0-4)

### Citations

**File:** sync/src/types/mod.rs (L1483-1532)
```rust
    pub fn add_ask_for_txs(&self, peer_index: PeerIndex, tx_hashes: Vec<Byte32>) -> Status {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();

        for tx_hash in tx_hashes
            .into_iter()
            .take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)
        {
            match unknown_tx_hashes.entry(tx_hash) {
                keyed_priority_queue::Entry::Occupied(entry) => {
                    let mut priority = entry.get_priority().clone();
                    priority.push_peer(peer_index);
                    entry.set_priority(priority);
                }
                keyed_priority_queue::Entry::Vacant(entry) => {
                    entry.set_priority(UnknownTxHashPriority {
                        request_time: Instant::now(),
                        peers: vec![peer_index],
                        requested: false,
                    })
                }
            }
        }

        // Check `unknown_tx_hashes`'s length after inserting the arrival `tx_hashes`
        if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
            || unknown_tx_hashes.len()
                >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
        {
            warn!(
                "unknown_tx_hashes is too long, len: {}",
                unknown_tx_hashes.len()
            );

            let mut peer_unknown_counter = 0;
            for (_hash, priority) in unknown_tx_hashes.iter() {
                for peer in priority.peers.iter() {
                    if *peer == peer_index {
                        peer_unknown_counter += 1;
                    }
                }
            }
            if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
                return StatusCode::TooManyUnknownTransactions.into();
            }

            return Status::ignored();
        }

        Status::ok()
    }
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L25-50)
```rust
    pub fn execute(self) -> Status {
        let state = self.relayer.shared().state();
        {
            let relay_transaction_hashes = self.message;
            if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    relay_transaction_hashes.tx_hashes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
        }

        let tx_hashes: Vec<_> = {
            let mut tx_filter = state.tx_filter();
            tx_filter.remove_expired();
            self.message
                .tx_hashes()
                .iter()
                .map(|x| x.to_entity())
                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                .collect()
        };

        state.add_ask_for_txs(self.peer, tx_hashes)
    }
```

**File:** util/constant/src/sync.rs (L67-72)
```rust
/// The maximum number transaction hashes inside a `RelayTransactionHashes` message
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
