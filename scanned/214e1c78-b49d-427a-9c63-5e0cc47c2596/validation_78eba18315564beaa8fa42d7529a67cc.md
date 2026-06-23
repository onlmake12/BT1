The code is now fully visible. Let me read the exact lines of the vulnerable function to confirm the scan behavior.

### Title
O(n) Global Mutex Scan in `SyncState::add_ask_for_txs` Triggered by Relay Peers — (`sync/src/types/mod.rs`)

### Summary

`SyncState::add_ask_for_txs` performs a full linear scan over the entire `unknown_tx_hashes` queue (up to 50,000 entries) while holding the `unknown_tx_hashes` `Mutex`, triggered whenever the queue reaches `MAX_UNKNOWN_TX_HASHES_SIZE`. Any unprivileged P2P peer can trigger this condition by sending `RelayTransactionHashes` messages in collusion with one other peer to fill the queue, causing repeated O(n) mutex hold times on every subsequent relay message.

### Finding Description

The function `add_ask_for_txs` in `sync/src/types/mod.rs` acquires the `unknown_tx_hashes` mutex at line 1484 and holds it for the entire function body. [1](#0-0) 

After inserting new hashes, it checks whether the queue has reached the global soft limit: [2](#0-1) 

When that condition is true, it performs a **full linear scan** over all entries in `unknown_tx_hashes` — still holding the mutex — to count how many entries belong to the calling peer: [3](#0-2) 

This is O(n) where n = `MAX_UNKNOWN_TX_HASHES_SIZE` = 50,000, with an inner loop over `priority.peers` per entry. [4](#0-3) 

The attacker-controlled entry point is `TransactionHashesProcess::execute`, which is invoked directly from the relay protocol handler for every incoming `RelayTransactionHashes` P2P message: [5](#0-4) 

The per-message batch limit is `MAX_RELAY_TXS_NUM_PER_BATCH` = 32,767: [6](#0-5) 

Two colluding peers each sending one full batch (2 × 32,767 = 65,534 > 50,000) is sufficient to push the queue past `MAX_UNKNOWN_TX_HASHES_SIZE`. Once the queue is saturated, **every** subsequent `RelayTransactionHashes` message from any peer triggers the O(n) scan while holding the mutex.

### Impact Explanation

While the mutex is held during the scan, all other callers of `unknown_tx_hashes.lock()` are blocked, including:

- `pop_ask_for_txs` — the relay thread's periodic tx dispatch loop
- `mark_as_known_txs` — called when transactions are verified or received [7](#0-6) [8](#0-7) 

The relay thread processes all relay protocol messages sequentially. Sustained mutex contention from repeated O(n) scans stalls transaction relay. Block propagation via compact blocks uses separate code paths and is not directly blocked, so the claim of "halting block propagation" is overstated — the accurate impact is **sustained CPU starvation of the relay thread's transaction relay path**, degrading transaction propagation throughput across the network.

### Likelihood Explanation

- Requires only two unprivileged P2P peers (no PoW, no keys, no privileged access).
- Each peer sends a single `RelayTransactionHashes` message with 32,767 novel hashes — a valid, protocol-conforming message.
- The queue stays saturated as long as the attacker peers remain connected and the node does not process/clear the hashes.
- The scan is re-triggered on every subsequent relay message from any peer once the queue is full, making this a sustained amplification attack.

### Recommendation

Maintain a per-peer counter in `PeerState` (incremented on insert, decremented on removal) rather than scanning the entire queue on every call. The check at line 1524 would then be O(1):

```rust
// In PeerState:
unknown_tx_hash_count: AtomicUsize,
```

This eliminates the O(n) scan entirely. The `take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)` guard at line 1488 already limits per-call insertion, but the post-insertion scan must also be O(1). [9](#0-8) 

### Proof of Concept

```
1. Connect two peers A and B to the target node.
2. Peer A sends RelayTransactionHashes with 32,767 novel tx hashes (valid message, passes the MAX_RELAY_TXS_NUM_PER_BATCH check).
3. Peer B sends RelayTransactionHashes with 32,767 different novel tx hashes.
4. Queue now has ~65,534 entries >= MAX_UNKNOWN_TX_HASHES_SIZE (50,000).
5. Peer A (or any peer) sends another RelayTransactionHashes message.
6. add_ask_for_txs acquires the mutex, inserts entries, hits the condition at line 1507, and iterates all 50,000+ entries while holding the lock.
7. Benchmark: measure lock hold time at step 6 — it scales linearly with queue size, not with the batch size of the incoming message.
8. Repeat step 5 at high frequency to sustain mutex contention and stall pop_ask_for_txs.
``` [10](#0-9)

### Citations

**File:** sync/src/types/mod.rs (L1443-1445)
```rust
    pub fn mark_as_known_txs(&self, hashes: impl Iterator<Item = Byte32> + std::clone::Clone) {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
        let mut tx_filter = self.tx_filter.lock();
```

**File:** sync/src/types/mod.rs (L1453-1454)
```rust
    pub fn pop_ask_for_txs(&self) -> HashMap<PeerIndex, Vec<Byte32>> {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
```

**File:** sync/src/types/mod.rs (L1483-1484)
```rust
    pub fn add_ask_for_txs(&self, peer_index: PeerIndex, tx_hashes: Vec<Byte32>) -> Status {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
```

**File:** sync/src/types/mod.rs (L1486-1488)
```rust
        for tx_hash in tx_hashes
            .into_iter()
            .take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)
```

**File:** sync/src/types/mod.rs (L1507-1528)
```rust
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
```

**File:** util/constant/src/sync.rs (L68-68)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
```

**File:** util/constant/src/sync.rs (L70-72)
```rust
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L49-50)
```rust
        state.add_ask_for_txs(self.peer, tx_hashes)
    }
```
