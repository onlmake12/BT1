### Title
Unbounded `peers` Vec Growth in `UnknownTxHashPriority` via Repeated `RelayTransactionHashes` — (`sync/src/types/mod.rs`)

### Summary
A single unprivileged remote peer can exhaust node memory by repeatedly sending `RelayTransactionHashes` messages containing the same set of tx hashes. Each repetition appends the peer's index to the `peers` Vec of every matching `UnknownTxHashPriority` entry with no cap, while the only size guard checks the number of **distinct hash entries** in the map — a value that never increases when the same hashes are re-announced.

### Finding Description

`push_peer()` appends unconditionally: [1](#0-0) 

In `add_ask_for_txs`, when a hash is already present (`Occupied`), the peer is pushed without any check on the current `peers` Vec length: [2](#0-1) 

The only size guard checks `unknown_tx_hashes.len()` — the count of **distinct hash keys** in the map: [3](#0-2) 

When the same peer re-announces the same K hashes, `unknown_tx_hashes.len()` stays constant at K (no new keys are inserted), so this guard **never fires**. The `peers` Vec for each of those K entries grows by 1 on every repeated message.

The `tx_filter` does not block this: hashes are only added to `tx_filter` via `mark_as_known_txs` after a transaction is resolved. Unresolved hashes in `unknown_tx_hashes` are invisible to `tx_filter`, so the same hash passes the filter check in `execute()` on every re-announcement: [4](#0-3) 

The per-message hash count is capped at `MAX_RELAY_TXS_NUM_PER_BATCH = 32767`: [5](#0-4) 

### Impact Explanation

Memory grows as `K × M × sizeof(PeerIndex)` where K = hashes per message (up to 32767) and M = number of repeated messages. At M = 10,000 messages with K = 32767 hashes: `32767 × 10,000 × 8 bytes ≈ 2.6 GB`. A single peer can drive the node to OOM and crash with no PoW, no stake, and no privileged access required.

### Likelihood Explanation

The attack requires only a standard P2P connection and the ability to send `RelayTransactionHashes` messages in a loop. No special peer status, no cryptographic material, and no coordination with other peers is needed. The hashes do not need to correspond to real transactions.

### Recommendation

Cap the `peers` Vec length in `push_peer` or in `add_ask_for_txs` before calling it. A reasonable bound is the maximum number of connected peers (already tracked in `self.peers.state.len()`). Additionally, deduplicate peer entries before appending, or skip the push if the peer is already present in the Vec. The `tx_filter` should also mark hashes as seen when they first enter `unknown_tx_hashes` to prevent the same peer from re-announcing the same hash repeatedly.

### Proof of Concept

```
1. Connect one peer to the node.
2. Send RelayTransactionHashes with 32767 novel hashes → unknown_tx_hashes.len() = 32767.
3. Send the identical RelayTransactionHashes message 10,000 more times.
4. unknown_tx_hashes.len() stays at 32767 (guard never fires).
5. Each entry's peers Vec has length 10,001.
6. Total PeerIndex allocations: 32767 × 10,001 × 8 bytes ≈ 2.6 GB.
7. Node OOM-crashes or becomes unresponsive.
```

### Citations

**File:** sync/src/types/mod.rs (L1291-1293)
```rust
    pub fn push_peer(&mut self, peer_index: PeerIndex) {
        self.peers.push(peer_index);
    }
```

**File:** sync/src/types/mod.rs (L1491-1494)
```rust
                keyed_priority_queue::Entry::Occupied(entry) => {
                    let mut priority = entry.get_priority().clone();
                    priority.push_peer(peer_index);
                    entry.set_priority(priority);
```

**File:** sync/src/types/mod.rs (L1507-1509)
```rust
        if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
            || unknown_tx_hashes.len()
                >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L38-49)
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
```

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
