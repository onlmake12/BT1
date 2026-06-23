### Title
O(n) Linear Scan Over `unknown_tx_hashes` on Every `RelayTransactionHashes` Message When Queue Is Full — (File: sync/src/types/mod.rs)

### Summary

When the global `unknown_tx_hashes` queue reaches its capacity (`MAX_UNKNOWN_TX_HASHES_SIZE` = 50,000 entries), every subsequent `RelayTransactionHashes` P2P message from **any** peer triggers a full O(n) linear scan over all entries in the queue while holding the queue's blocking mutex. An unprivileged attacker with two or more peer connections can pre-fill the queue with fake transaction hashes and then repeatedly send cheap relay messages to force continuous O(n) scans, blocking the mutex and starving legitimate relay and tx-pool operations.

---

### Finding Description

`SyncState::add_ask_for_txs` is the function that processes incoming `RelayTransactionHashes` relay messages. It inserts announced tx hashes into the shared `unknown_tx_hashes: Mutex<KeyedPriorityQueue<Byte32, UnknownTxHashPriority>>` and then checks whether the queue has grown too large: [1](#0-0) 

After insertion, when either size threshold is met, the code performs a **full linear scan** over every entry in the queue to count how many entries belong to the calling peer: [2](#0-1) 

```rust
let mut peer_unknown_counter = 0;
for (_hash, priority) in unknown_tx_hashes.iter() {   // O(n) — up to 50 000 entries
    for peer in priority.peers.iter() {                // O(m) — unbounded Vec<PeerIndex>
        if *peer == peer_index {
            peer_unknown_counter += 1;
        }
    }
}
```

The entire function holds the blocking `unknown_tx_hashes` mutex from entry to return: [3](#0-2) 

The two capacity constants that govern when the scan fires are: [4](#0-3) 

```
MAX_UNKNOWN_TX_HASHES_SIZE          = 50 000
MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32 767   (== MAX_RELAY_TXS_NUM_PER_BATCH)
```

**Attack pre-condition — filling the queue without being banned:**

A single peer that contributes ≥ 32,767 entries is detected and banned (the `TooManyUnknownTransactions` path). However, with **two peers** each contributing ~25,000 distinct fake tx hashes, the queue reaches 50,000 entries while each peer's individual counter stays below the ban threshold. The check at line 1507 fires (`len >= MAX_UNKNOWN_TX_HASHES_SIZE`), the O(n) scan runs, `peer_unknown_counter` is 25,000 < 32,767, and the function returns `Status::ignored()` — **no ban**. [5](#0-4) 

Once the queue is saturated, **every** subsequent `RelayTransactionHashes` message from any peer (attacker or legitimate) re-enters the scan path. The attacker can sustain this by periodically re-announcing the same fake hashes (entries are evicted from the queue after all their associated peers have been tried, so the attacker simply reconnects or uses additional peers to keep the queue full).

The `peers` field inside each `UnknownTxHashPriority` is an unbounded `Vec<PeerIndex>`: [6](#0-5) 

If many peers announce the same hash, `peers` grows, making the inner loop proportionally slower and amplifying the total work per scan.

The entry point from the P2P relay protocol is: [7](#0-6) 

Any unauthenticated peer on the relay protocol can send this message.

---

### Impact Explanation

While the queue is saturated, every `RelayTransactionHashes` message — including those from honest peers — causes:

1. **CPU burn**: up to 50,000 × (peers-per-entry) comparisons per message, under a blocking mutex.
2. **Mutex starvation**: `pop_ask_for_txs`, `mark_as_known_txs`, and `unknown_tx_hashes()` all contend on the same `Mutex`. Prolonged lock-hold delays tx-request scheduling and tx-filter updates, degrading relay throughput.
3. **Amplification**: the attacker pays only the cost of sending a small `RelayTransactionHashes` message (≤ 32,767 hashes × 32 bytes ≈ 1 MB) to force O(50,000) work on the victim node.

---

### Likelihood Explanation

- **Entry path**: any unprivileged peer on the `RelayV3` protocol; no authentication required.
- **Setup cost**: two TCP connections and two batches of fake 32-byte hashes (≈ 1.6 MB total) are sufficient to saturate the queue.
- **Sustainability**: the attacker keeps the queue full by periodically re-announcing hashes before they are evicted; the node cannot distinguish fake hashes from real ones at announcement time.
- **No ban risk**: as shown above, each individual peer stays below the per-peer ban threshold.

---

### Recommendation

1. **Maintain a per-peer counter map** (`HashMap<PeerIndex, usize>`) updated incrementally on insert/remove, so the O(n) scan is replaced by an O(1) lookup.
2. **Bound `UnknownTxHashPriority::peers`** to a small constant (e.g., 8) to cap the inner-loop cost even if the outer loop cannot be avoided.
3. **Drop entries whose all peers have been exhausted** eagerly rather than relying on the priority queue's natural eviction, to keep the queue size well below the threshold in normal operation.

---

### Proof of Concept

```
1. Attacker opens two peer connections (peer_A, peer_B) to the target node.

2. peer_A sends RelayTransactionHashes with 25 000 distinct fake tx hashes.
   → unknown_tx_hashes grows to 25 000; no threshold crossed; no ban.

3. peer_B sends RelayTransactionHashes with 25 000 distinct fake tx hashes.
   → unknown_tx_hashes grows to 50 000.
   → Threshold fires: O(50 000) scan runs; peer_B counter = 25 000 < 32 767 → ignored, not banned.

4. Attacker now sends RelayTransactionHashes from peer_A (or any third peer) at high rate.
   → Each message: mutex acquired → O(50 000) scan → mutex released.
   → Legitimate relay operations (pop_ask_for_txs, mark_as_known_txs) are starved.

5. Attacker periodically re-announces hashes to replenish evicted entries and keep the
   queue at capacity, sustaining the condition indefinitely.
```

### Citations

**File:** sync/src/types/mod.rs (L1257-1260)
```rust
pub struct UnknownTxHashPriority {
    request_time: Instant,
    peers: Vec<PeerIndex>,
    requested: bool,
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

**File:** util/constant/src/sync.rs (L70-72)
```rust
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
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
