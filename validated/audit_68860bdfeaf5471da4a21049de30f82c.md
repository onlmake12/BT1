### Title
Incomplete Peer State Cleanup on Disconnect Leaves Stale Relay State in `SyncState`, Degrading Transaction Propagation - (File: `sync/src/types/mod.rs`)

---

### Summary

When a peer disconnects, `SyncState::disconnected()` only removes `inflight_blocks` and `peers.state`, but leaves the disconnected peer's `PeerIndex` embedded in three other shared relay data structures: `unknown_tx_hashes`, `pending_compact_blocks`, and `pending_get_block_proposals`. The stale `unknown_tx_hashes` entries are the most impactful: because the per-peer admission threshold is computed as `peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, a peer that fills its quota and then disconnects shrinks the threshold for all remaining peers, causing legitimate `RelayTransactionHashes` messages from honest peers to be silently dropped.

---

### Finding Description

`SyncState::disconnected()` is the single cleanup hook called by `Synchronizer::disconnected()` on peer departure:

```rust
pub fn disconnected(&self, pi: PeerIndex) {
    let removed_inflight_blocks_count = self.write_inflight_blocks().remove_by_peer(pi);
    ...
    self.peers().disconnected(pi);
}
```

It cleans up exactly two structures: `inflight_blocks` and `peers.state`. The following peer-indexed relay structures are **never** cleaned up on disconnect:

**1. `unknown_tx_hashes: Mutex<KeyedPriorityQueue<Byte32, UnknownTxHashPriority>>`**

`UnknownTxHashPriority` embeds a `peers: Vec<PeerIndex>`. When a peer sends `RelayTransactionHashes`, its `PeerIndex` is pushed into this vector via `add_ask_for_txs`. On disconnect, those entries are never removed. The admission check in `add_ask_for_txs` is:

```rust
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
    || unknown_tx_hashes.len()
        >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
```

After the attacker disconnects, `peers.state.len()` drops by 1 but `unknown_tx_hashes.len()` does not. The dynamic threshold `peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` shrinks, causing subsequent honest peers' `RelayTransactionHashes` messages to hit the overflow branch and be silently dropped (`Status::ignored()`).

**2. `pending_compact_blocks: tokio::sync::Mutex<PendingCompactBlockMap>`**

`PendingCompactBlockMap` is `HashMap<Byte32, (CompactBlock, HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>, u64)>`. When a compact block requires missing transactions, the peer's `PeerIndex` is inserted into the inner map via `missing_or_collided_post_process`. On disconnect, this entry is never removed. The compact block payload (potentially large) is retained in memory until the next successful block reconstruction triggers the epoch-based `retain` sweep. A peer that repeatedly sends compact blocks requiring missing transactions and then disconnects can cause unbounded memory growth between epoch transitions.

**3. `pending_get_block_proposals: DashMap<ProposalShortId, HashSet<PeerIndex>>`**

When a peer sends `GetBlockProposal`, its `PeerIndex` is inserted into the `HashSet` via `insert_get_block_proposals`. On disconnect, the `PeerIndex` remains. `prune_tx_proposal_request` drains this map and attempts to send `BlockProposal` messages to every stored `PeerIndex`, including disconnected ones. If a `PeerIndex` is reused by a new peer (session IDs are sequential integers and wrap), the new peer receives unsolicited block proposal data it never requested.

The `Relayer::disconnected()` handler is also relevant: it does **not** call `sync_state.disconnected()` at all, only calling `rate_limiter.retain_recent()`. All relay-layer cleanup is therefore entirely absent from the relay disconnect path.

---

### Impact Explanation

The primary impact is on transaction propagation. An attacker fills `unknown_tx_hashes` to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` entries and disconnects. The dynamic threshold shrinks, causing honest peers' `RelayTransactionHashes` messages to be silently dropped. Repeated connect-fill-disconnect cycles by multiple attackers can keep the threshold permanently suppressed, preventing the node from learning about new transactions from its honest peers. Secondary impacts include memory bloat from retained compact block payloads and spurious `BlockProposal` messages sent to reused session IDs.

---

### Likelihood Explanation

The entry path requires only an unprivileged P2P connection. Any peer reachable by the node can send `RelayTransactionHashes` up to the per-peer limit and then disconnect. No authentication, no special privileges, and no majority hashpower are required. The attack is repeatable and low-cost.

---

### Recommendation

Add cleanup for all three structures inside `SyncState::disconnected()`:

1. Scan `unknown_tx_hashes` and remove entries whose `peers` vector contains only the disconnecting `PeerIndex`, or remove the `PeerIndex` from the vector for entries that have multiple peers.
2. Iterate `pending_compact_blocks` and remove the disconnecting `PeerIndex` from each inner `HashMap<PeerIndex, ...>`, dropping the outer entry if the inner map becomes empty.
3. Iterate `pending_get_block_proposals` and remove the disconnecting `PeerIndex` from each `HashSet<PeerIndex>`, dropping the outer entry if the set becomes empty.

---

### Proof of Concept

1. Connect to a CKB node as an unprivileged relay peer.
2. Send `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` distinct `RelayTransactionHashes` messages (each with unique, unseen tx hashes). Each call to `add_ask_for_txs` inserts the attacker's `PeerIndex` into `unknown_tx_hashes`.
3. Disconnect. `Synchronizer::disconnected()` calls `sync_state.disconnected()`, which removes the peer from `peers.state` (decrementing `peers.state.len()`) but leaves all `unknown_tx_hashes` entries intact.
4. The threshold `peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` has now decreased by exactly `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, while `unknown_tx_hashes.len()` is unchanged.
5. An honest peer now sends a single `RelayTransactionHashes` message. `add_ask_for_txs` evaluates `unknown_tx_hashes.len() >= peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, finds the condition true, and returns `Status::ignored()` — the honest peer's transaction hashes are silently discarded without the node ever requesting the transactions.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** sync/src/types/mod.rs (L1318-1341)
```rust
pub struct SyncState {
    /* Status irrelevant to peers */
    shared_best_header: RwLock<HeaderIndexView>,
    tx_filter: Mutex<TtlFilter<Byte32>>,

    // The priority is ordering by timestamp (reversed), means do not ask the tx before this timestamp (timeout).
    unknown_tx_hashes: Mutex<KeyedPriorityQueue<Byte32, UnknownTxHashPriority>>,

    /* Status relevant to peers */
    peers: Peers,

    /* Cached items which we had received but not completely process */
    pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
    pending_get_headers: RwLock<LruCache<(PeerIndex, Byte32), Instant>>,
    pending_compact_blocks: tokio::sync::Mutex<PendingCompactBlockMap>,

    /* In-flight items for which we request to peers, but not got the responses yet */
    inflight_proposals: DashMap<packed::ProposalShortId, BlockNumber>,
    inflight_blocks: RwLock<InflightBlocks>,

    /* cached for sending bulk */
    tx_relay_receiver: Receiver<TxVerificationResult>,
    min_chain_work: U256,
}
```

**File:** sync/src/types/mod.rs (L1483-1531)
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
```

**File:** sync/src/types/mod.rs (L1607-1616)
```rust
    pub fn disconnected(&self, pi: PeerIndex) {
        let removed_inflight_blocks_count = self.write_inflight_blocks().remove_by_peer(pi);
        if removed_inflight_blocks_count > 0 {
            debug!(
                "disconnected {}, remove {} inflight blocks",
                pi, removed_inflight_blocks_count
            )
        }
        self.peers().disconnected(pi);
    }
```

**File:** sync/src/relayer/mod.rs (L923-935)
```rust
    async fn disconnected(
        &mut self,
        _nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
    ) {
        info_target!(
            crate::LOG_TARGET_RELAY,
            "RelayProtocol.disconnected peer={}",
            peer_index
        );
        // Retains all keys in the rate limiter that were used recently enough.
        self.rate_limiter.retain_recent();
    }
```

**File:** sync/src/synchronizer/mod.rs (L982-990)
```rust
    async fn disconnected(
        &mut self,
        _nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
    ) {
        let sync_state = self.shared().state();
        sync_state.disconnected(peer_index);
        info!("SyncProtocol.disconnected peer={}", peer_index);
    }
```
