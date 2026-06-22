### Title
O(n) Full Linear Scan of `unknown_tx_hashes` Under Global Mutex on Every Inbound `RelayTransactionHashes` Message When Queue Is Saturated — (`sync/src/types/mod.rs`)

---

### Summary

Once an attacker saturates `unknown_tx_hashes` to ≥ `MAX_UNKNOWN_TX_HASHES_SIZE` (50 000) entries, every subsequent call to `add_ask_for_txs` — from **any** peer — performs an O(n) full linear scan of the entire queue while holding the global `unknown_tx_hashes` `Mutex`. The scan is not bounded by the incoming message size; it is bounded by the total queue depth, which can grow well beyond 50 000 because the size check fires **after** insertion and the non-offending path returns `Status::ignored()` without evicting the just-inserted hashes.

---

### Finding Description

**Entry point:**
`TransactionHashesProcess::execute` in `sync/src/relayer/transaction_hashes_process.rs` calls `state.add_ask_for_txs(self.peer, tx_hashes)` after filtering already-known hashes. [1](#0-0) 

**Mutex acquisition:**
`add_ask_for_txs` immediately acquires `unknown_tx_hashes.lock()` and holds it for the entire function body. [2](#0-1) 

**Insertion before the guard:**
Up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` = 32 767 hashes are unconditionally inserted into the queue **before** any size check. [3](#0-2) 

**Post-insertion size check:**
Only after insertion does the code check whether the queue has reached `MAX_UNKNOWN_TX_HASHES_SIZE` (50 000). [4](#0-3) 

**O(n) scan under the held mutex:**
When the threshold is exceeded, the code iterates over every entry in the queue — O(total queue depth) — to count how many entries belong to the current peer. [5](#0-4) 

**No eviction on the non-offending path:**
If the per-peer counter is below the limit, the function returns `Status::ignored()` — the just-inserted hashes **remain** in the queue, and no entries are removed. The queue therefore grows monotonically with each call once the threshold is crossed. [6](#0-5) 

**Constants:** [7](#0-6) 

---

### Impact Explanation

While the mutex is held for the O(n) scan, `pop_ask_for_txs` (which also acquires `unknown_tx_hashes.lock()`) is blocked. `pop_ask_for_txs` is the function that actually dispatches `GetTransactions` requests to peers; stalling it stalls the entire relay transaction-fetching pipeline for all peers simultaneously. Because `Status::ignored()` does not evict hashes, the queue depth — and therefore the scan cost — grows with each new peer that sends hashes after saturation, compounding the stall duration over time.

---

### Likelihood Explanation

The attack requires only two cooperating peers:

1. **Peer A** sends 32 767 unique hashes → queue = 32 767 (below threshold, no scan).
2. **Peer B** sends 32 767 unique hashes → queue = 65 534 (≥ 50 000, O(65 534) scan fires; peer B hits the per-peer limit and is disconnected, but its hashes remain).
3. **Peer C** (or a reconnected peer) sends 32 767 more unique hashes → queue = 98 301, O(98 301) scan fires, returns `Status::ignored()`, hashes stay.
4. Repeat step 3 indefinitely.

No PoW, no privileged role, no leaked key is required. Any unprivileged P2P peer can participate.

---

### Recommendation

1. **Check before inserting:** Move the size/per-peer guard to the top of `add_ask_for_txs`, before the insertion loop, so hashes from an over-quota peer are never added.
2. **Eliminate the O(n) scan:** Maintain a per-peer counter as a side-structure (e.g., a `HashMap<PeerIndex, usize>`) updated incrementally during insertion, so the per-peer check is O(1).
3. **Evict on overflow:** When the global limit is reached, evict the lowest-priority entries rather than silently accepting them and returning `ignored`.

---

### Proof of Concept

```rust
// Pseudocode unit test
let state = SyncState::new(...);

// Step 1: fill queue past MAX_UNKNOWN_TX_HASHES_SIZE using peer_a and peer_b
let hashes_a: Vec<Byte32> = (0..32767).map(|i| make_hash(i)).collect();
let hashes_b: Vec<Byte32> = (32767..65534).map(|i| make_hash(i)).collect();
state.add_ask_for_txs(peer_a, hashes_a);
state.add_ask_for_txs(peer_b, hashes_b); // queue now ~65534, scan fires

// Step 2: measure latency of subsequent calls from peer_c
let hashes_c: Vec<Byte32> = (65534..65566).map(|i| make_hash(i)).collect();
let t0 = Instant::now();
state.add_ask_for_txs(peer_c, hashes_c); // triggers O(65534+) scan
let elapsed = t0.elapsed();

// Assert: elapsed should be O(1) relative to queue size, not O(65534)
assert!(elapsed < Duration::from_micros(100), "O(n) scan detected: {:?}", elapsed);
```

### Citations

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

**File:** sync/src/types/mod.rs (L1483-1484)
```rust
    pub fn add_ask_for_txs(&self, peer_index: PeerIndex, tx_hashes: Vec<Byte32>) -> Status {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
```

**File:** sync/src/types/mod.rs (L1486-1503)
```rust
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
```

**File:** sync/src/types/mod.rs (L1506-1514)
```rust
        // Check `unknown_tx_hashes`'s length after inserting the arrival `tx_hashes`
        if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
            || unknown_tx_hashes.len()
                >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
        {
            warn!(
                "unknown_tx_hashes is too long, len: {}",
                unknown_tx_hashes.len()
            );
```

**File:** sync/src/types/mod.rs (L1516-1523)
```rust
            let mut peer_unknown_counter = 0;
            for (_hash, priority) in unknown_tx_hashes.iter() {
                for peer in priority.peers.iter() {
                    if *peer == peer_index {
                        peer_unknown_counter += 1;
                    }
                }
            }
```

**File:** sync/src/types/mod.rs (L1524-1528)
```rust
            if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
                return StatusCode::TooManyUnknownTransactions.into();
            }

            return Status::ignored();
```

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
