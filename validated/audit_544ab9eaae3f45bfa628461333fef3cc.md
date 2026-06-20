### Title
Unbounded O(n) Scan Over `unknown_tx_hashes` Under Mutex Lock Enables Relay DoS - (`File: sync/src/types/mod.rs`)

### Summary

The `add_ask_for_txs` function in `sync/src/types/mod.rs` performs an unbounded linear scan over the entire `unknown_tx_hashes` priority queue (up to `MAX_UNKNOWN_TX_HASHES_SIZE` = 50,000 entries) while holding the `unknown_tx_hashes` mutex lock, every time the queue is at or above capacity. An unprivileged remote peer can fill the queue to capacity and then repeatedly send `RelayTransactionHashes` messages to continuously trigger this O(n·m) scan, stalling all other operations that require the same mutex.

### Finding Description

`SyncState` maintains a global `unknown_tx_hashes: Mutex<KeyedPriorityQueue<Byte32, UnknownTxHashPriority>>` that tracks transaction hashes announced by peers but not yet fetched. Each entry holds an `UnknownTxHashPriority` containing a `peers: Vec<PeerIndex>` that grows via `push_peer` with no per-entry bound. [1](#0-0) 

When `add_ask_for_txs` is called (triggered by any peer's `RelayTransactionHashes` message), it first inserts up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (= 32,767) hashes into the queue. After insertion, if the queue length meets or exceeds `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000) or the per-peer soft limit, it performs a full O(n·m) scan over every entry and every peer within each entry's `peers` Vec — all while holding the mutex: [2](#0-1) 

The scan is not bounded or short-circuited early. It counts how many times `peer_index` appears across all 50,000 entries before deciding whether to reject or ignore the message. This is analogous to the Opyn `userDepositsIndex` pattern: the index (queue) grows to a fixed large size, and every subsequent operation must traverse the entire structure.

The `push_peer` call at line 1292 has no bound on how many peers can accumulate in a single entry's `peers` Vec: [3](#0-2) 

The entry point is `TransactionHashesProcess::execute`, reachable from any connected relay peer: [4](#0-3) 

The per-message hash count is validated against `MAX_RELAY_TXS_NUM_PER_BATCH`: [5](#0-4) 

But there is no rate limit on how frequently a peer may send `RelayTransactionHashes` messages, and no throttle on how often the O(n) overflow scan fires.

The constants involved: [6](#0-5) 

### Impact Explanation

Every time the queue is at capacity and a peer sends a `RelayTransactionHashes` message, the node performs an O(50,000 × peers_per_entry) scan while holding the `unknown_tx_hashes` mutex. All concurrent callers of `pop_ask_for_txs`, `mark_as_known_txs`, `mark_as_known_tx`, `remove_from_known_txs`, and `add_ask_for_txs` are blocked for the duration of the scan. With two cooperating peers each sending one batch of 32,767 hashes, the queue reaches capacity. Thereafter, every subsequent message from any peer triggers the full scan. This degrades transaction relay throughput and can cause the relay subsystem to stall under sustained message flood.

### Likelihood Explanation

Any unprivileged peer that has established a relay protocol connection can send `RelayTransactionHashes` messages. No special privilege, key, or majority hashpower is required. Two peers are sufficient to fill the queue to capacity. The attack is repeatable at network message rate with no per-peer send-rate enforcement in the handler.

### Recommendation

1. **Short-circuit the overflow scan**: maintain a per-peer counter map alongside `unknown_tx_hashes` so the per-peer count can be looked up in O(1) instead of scanning all entries.
2. **Bound `peers` Vec per entry**: cap the number of peers stored in `UnknownTxHashPriority::peers` (e.g., at the number of connected peers or a fixed small constant) and reject `push_peer` when the cap is reached.
3. **Rate-limit `RelayTransactionHashes` per peer**: track the last message timestamp per peer and drop messages that arrive too frequently.

### Proof of Concept

1. Attacker controls two relay peers, Peer A and Peer B, connected to the victim node.
2. Peer A sends a `RelayTransactionHashes` message with 32,767 unique, never-seen tx hashes → `unknown_tx_hashes` grows to 32,767 entries.
3. Peer B sends a `RelayTransactionHashes` message with 32,767 more unique hashes → `unknown_tx_hashes` reaches ≥ 50,000 (capacity).
4. Peer A (or any peer) repeatedly sends `RelayTransactionHashes` messages with any hashes. Each message triggers the overflow check at line 1507, which iterates all 50,000 entries while holding the mutex.
5. The victim node's relay thread is continuously occupied with O(50,000) scans under the mutex, blocking `pop_ask_for_txs` and `mark_as_known_txs`, stalling transaction relay for all peers. [7](#0-6)

### Citations

**File:** sync/src/types/mod.rs (L1256-1293)
```rust
#[derive(Eq, PartialEq, Clone)]
pub struct UnknownTxHashPriority {
    request_time: Instant,
    peers: Vec<PeerIndex>,
    requested: bool,
}

impl UnknownTxHashPriority {
    pub fn should_request(&self, now: Instant) -> bool {
        self.next_request_at() < now
    }

    pub fn next_request_at(&self) -> Instant {
        if self.requested {
            self.request_time + RETRY_ASK_TX_TIMEOUT_INCREASE
        } else {
            self.request_time
        }
    }

    pub fn next_request_peer(&mut self) -> Option<PeerIndex> {
        if self.requested {
            if self.peers.len() > 1 {
                self.request_time = Instant::now();
                self.peers.swap_remove(0);
                self.peers.first().cloned()
            } else {
                None
            }
        } else {
            self.requested = true;
            self.peers.first().cloned()
        }
    }

    pub fn push_peer(&mut self, peer_index: PeerIndex) {
        self.peers.push(peer_index);
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

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
