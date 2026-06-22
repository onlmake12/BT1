### Title
Peer Disconnect Does Not Purge `unknown_tx_hashes` Entries, Causing Stale State Accumulation and Transaction Propagation Disruption - (File: sync/src/types/mod.rs)

---

### Summary

`SyncState::disconnected()` removes a peer from `inflight_blocks` and `peers` state but does not purge the disconnected peer's entries from `unknown_tx_hashes`. This is a direct state machine inconsistency: the peer is deregistered from one sub-state but its associated relay-request state persists indefinitely. A malicious unprivileged peer can exploit this to exhaust the `unknown_tx_hashes` queue, causing legitimate peers' transaction hash announcements to be silently dropped, disrupting transaction propagation across the node.

---

### Finding Description

`SyncState` in `sync/src/types/mod.rs` maintains several peer-associated data structures:

- `peers` — per-peer sync state
- `inflight_blocks` — in-flight block download requests per peer
- `unknown_tx_hashes: Mutex<KeyedPriorityQueue<Byte32, UnknownTxHashPriority>>` — pending transaction hash fetch requests, where each entry's `UnknownTxHashPriority` contains a `peers: Vec<PeerIndex>` list of peers that announced the hash [1](#0-0) 

When a peer disconnects, `SyncState::disconnected()` is called:

```rust
pub fn disconnected(&self, pi: PeerIndex) {
    let removed_inflight_blocks_count = self.write_inflight_blocks().remove_by_peer(pi);
    // ...
    self.peers().disconnected(pi);
}
``` [2](#0-1) 

It cleans up `inflight_blocks` and `peers`, but **never touches `unknown_tx_hashes`**. Entries inserted by `add_ask_for_txs()` for the disconnected peer remain in the queue indefinitely. [3](#0-2) 

The rate-limiting guard in `add_ask_for_txs()` compares the total queue length (including stale disconnected-peer entries) against the **current** connected peer count:

```rust
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
    || unknown_tx_hashes.len()
        >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
{
    // ...
    return Status::ignored();
}
``` [4](#0-3) 

Because `self.peers.state.len()` reflects only currently connected peers while `unknown_tx_hashes.len()` includes stale entries from all previously connected peers, the effective per-peer budget shrinks as peers cycle through. Once the queue is sufficiently polluted, legitimate peers' `RelayTransactionHashes` messages return `Status::ignored()`, meaning the node never fetches those transactions.

The entry point is `TransactionHashesProcess::execute()`, which calls `state.add_ask_for_txs(self.peer, tx_hashes)` for every `RelayTransactionHashes` message received from any peer: [5](#0-4) 

The `Peers::disconnected()` method removes the peer from `peers.state` but has no knowledge of `unknown_tx_hashes`: [6](#0-5) 

---

### Impact Explanation

An attacker operating as an unprivileged relay peer can:

1. Connect to a victim node.
2. Send `RelayTransactionHashes` messages advertising up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` unknown tx hashes, filling `unknown_tx_hashes` with entries attributed to their peer index.
3. Disconnect. The entries remain in `unknown_tx_hashes`.
4. Repeat with new connections.

After enough cycles, `unknown_tx_hashes.len()` exceeds `self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, causing all subsequent `add_ask_for_txs()` calls from legitimate peers to return `Status::ignored()`. The victim node stops fetching newly announced transactions from honest peers, disrupting mempool propagation. The node's tx-pool becomes isolated from the network's pending transaction set without any error or ban being triggered.

---

### Likelihood Explanation

The attack requires only the ability to open and close P2P connections to the target node, which is available to any unprivileged network peer. No special privileges, keys, or majority hashpower are needed. The `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` constant bounds how many entries each connection contributes, but since entries are never evicted on disconnect, the attacker simply needs enough connection cycles to fill the queue. The attack is repeatable and low-cost.

---

### Recommendation

In `SyncState::disconnected()`, after removing inflight blocks and peer state, iterate over `unknown_tx_hashes` and remove all entries whose `peers` list becomes empty after removing the disconnected peer index, or remove the peer index from each entry's `peers` vec. This mirrors the cleanup already performed for `inflight_blocks`:

```rust
pub fn disconnected(&self, pi: PeerIndex) {
    let removed_inflight_blocks_count = self.write_inflight_blocks().remove_by_peer(pi);
    // ... existing logging ...
    
    // NEW: purge stale unknown_tx_hashes entries for disconnected peer
    let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
    unknown_tx_hashes.retain(|_hash, priority| {
        priority.peers.retain(|p| *p != pi);
        !priority.peers.is_empty()
    });
    
    self.peers().disconnected(pi);
}
```

Additionally, add a fuzz/property test asserting that after `disconnected(pi)`, no entry in `unknown_tx_hashes` references `pi`.

---

### Proof of Concept

1. Connect a malicious peer to the victim CKB node via the Relay protocol.
2. Send a `RelayTransactionHashes` message containing `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` fabricated (non-existent) tx hashes. These are inserted into `unknown_tx_hashes` via `add_ask_for_txs`.
3. Disconnect the malicious peer. Observe that `SyncState::disconnected()` does not clear the entries from `unknown_tx_hashes`.
4. Repeat steps 1–3 until `unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE` or `>= peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`.
5. Connect a legitimate peer and have it send a `RelayTransactionHashes` message. Observe that `add_ask_for_txs` returns `Status::ignored()` and the victim node never sends `GetRelayTransactions` for those hashes — confirmed by the absence of any outbound `GetRelayTransactions` message to the legitimate peer. [7](#0-6) [5](#0-4) [2](#0-1)

### Citations

**File:** sync/src/types/mod.rs (L901-924)
```rust
    pub fn disconnected(&self, peer: PeerIndex) {
        if let Some(peer_state) = self.state.remove(&peer).map(|(_, peer_state)| peer_state) {
            if peer_state.sync_started() {
                // It shouldn't happen
                // fetch_sub wraps around on overflow, we still check manually
                // panic here to prevent some bug be hidden silently.
                assert_ne!(
                    self.n_sync_started.fetch_sub(1, Ordering::AcqRel),
                    0,
                    "n_sync_started overflow when disconnects"
                );
            }

            // Protection node disconnected
            if peer_state.peer_flags.is_protect {
                assert_ne!(
                    self.n_protected_outbound_peers
                        .fetch_sub(1, Ordering::AcqRel),
                    0,
                    "n_protected_outbound_peers overflow when disconnects"
                );
            }
        }
    }
```

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

**File:** sync/src/relayer/transaction_hashes_process.rs (L38-50)
```rust
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
