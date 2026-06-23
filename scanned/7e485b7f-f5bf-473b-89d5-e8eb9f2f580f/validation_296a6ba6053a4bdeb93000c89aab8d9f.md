### Title
Unbounded `peers` Vector Growth in `UnknownTxHashPriority::push_peer` Enables CPU Exhaustion via Nested Iteration — (`File: sync/src/types/mod.rs`)

### Summary

`UnknownTxHashPriority::push_peer` appends a peer index to an internal `Vec<PeerIndex>` with no deduplication and no size cap. A malicious peer can repeatedly re-announce the same transaction hashes via `RelayTransactionHashes` P2P messages, causing the `peers` vector inside each `unknown_tx_hashes` entry to grow without bound. When a subsequent announcement from any peer triggers the size-overflow branch in `add_ask_for_txs`, a nested loop iterates over every entry and every element of every `peers` vector while holding the `unknown_tx_hashes` mutex, causing sustained CPU exhaustion proportional to the accumulated vector size.

### Finding Description

**Root cause — `push_peer` has no deduplication or size limit:** [1](#0-0) 

```rust
pub fn push_peer(&mut self, peer_index: PeerIndex) {
    self.peers.push(peer_index);
}
```

This is called unconditionally from `add_ask_for_txs` whenever a tx hash already exists in the map: [2](#0-1) 

The size guard that follows checks only `unknown_tx_hashes.len()` — the number of *distinct* tx-hash keys — not the length of the `peers` vector inside each entry: [3](#0-2) 

Because re-announcing the same hashes does not add new keys, the guard is never triggered by repeated announcements of identical hashes. The `peers` vector therefore grows without bound.

**Expensive nested loop triggered by the size guard:**

When the guard *does* fire (e.g., a second peer pushes the total key count over `MAX_UNKNOWN_TX_HASHES_SIZE = 50 000`), the code executes: [4](#0-3) 

```rust
for (_hash, priority) in unknown_tx_hashes.iter() {   // O(entries)
    for peer in priority.peers.iter() {                // O(peers_per_entry)
        if *peer == peer_index { peer_unknown_counter += 1; }
    }
}
```

This runs while holding the `unknown_tx_hashes` `Mutex` lock. Total work is `O(entries × max_peers_per_entry)`. If an attacker has pre-inflated each entry's `peers` vector to length K, the work is `O(50 000 × K)` — unbounded in K.

**`UnknownTxHashPriority` struct and the `peers` field:** [5](#0-4) 

**Constants governing the limits:** [6](#0-5) 

`MAX_RELAY_TXS_NUM_PER_BATCH = 32 767`, `MAX_UNKNOWN_TX_HASHES_SIZE = 50 000`, `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32 767`.

**Entry point — `transaction_hashes_process.rs` calls `add_ask_for_txs`:** [7](#0-6) 

Any connected peer can send `RelayTransactionHashes` P2P messages at will; there is no per-peer rate limit on this message type.

### Impact Explanation

An attacker holding two peer connections (A and B) can:

1. Peer A sends 25 000 unique tx hashes → 25 000 entries created, each `peers = [A]`.
2. Peer A re-sends the same 25 000 hashes K times → each entry's `peers` vector grows to length K+1; `unknown_tx_hashes.len()` stays at 25 000 (no new keys), so the guard never fires.
3. Peer B sends 25 000 different unique hashes → total keys reach 50 000, triggering the guard.
4. The nested loop now performs `25 000 × (K+1) + 25 000 × 1 ≈ 25 000 × K` comparisons while holding the mutex.

At K = 10 000 (10 000 repeated `RelayTransactionHashes` messages from peer A, each carrying 25 000 hashes), the loop executes ~250 million iterations under the lock. This stalls every other operation that acquires `unknown_tx_hashes`, including `pop_ask_for_txs` and `mark_as_known_txs`, degrading transaction relay for all peers connected to the victim node. The attack is repeatable: after the mutex is released the attacker can re-inflate and re-trigger.

**Impact class:** Sustained CPU exhaustion / partial denial-of-service of the transaction relay subsystem. Severity: Medium.

### Likelihood Explanation

- Requires only two ordinary peer connections — no keys, no stake, no privileged role.
- `RelayTransactionHashes` is a standard relay protocol message accepted from any peer.
- No rate limit exists on how often a peer may re-announce the same hashes.
- The attack is cheap: each message is small (32-byte hashes × 32 767 = ~1 MB) and the attacker pays no on-chain cost.
- Effective on any CKB node that accepts inbound relay connections (the default configuration).

### Recommendation

1. **Deduplicate in `push_peer`:** Before pushing, check whether `peer_index` is already present (or use a `HashSet<PeerIndex>` instead of `Vec<PeerIndex>`).

```rust
pub fn push_peer(&mut self, peer_index: PeerIndex) {
    if !self.peers.contains(&peer_index) {
        self.peers.push(peer_index);
    }
}
```

2. **Cap the `peers` vector:** Enforce a hard maximum (e.g., equal to the maximum number of connected peers) so the vector cannot grow beyond a known bound regardless of deduplication logic.

3. **Move the per-peer counter to a separate `DashMap<PeerIndex, usize>`:** Avoid the O(entries × peers) nested scan entirely by maintaining an explicit per-peer count that is incremented/decremented as entries are added or removed.

### Proof of Concept

**Attacker-controlled steps (no privileged access required):**

1. Connect two peers A and B to the victim CKB node using the standard Relay v3 protocol.
2. From peer A, send repeated `RelayTransactionHashes` messages each containing the same 25 000 fabricated (non-existent) tx hashes. Repeat K = 10 000 times. Each call to `add_ask_for_txs` finds existing entries and calls `push_peer(A)` 25 000 times, growing each entry's `peers` vector by 1. No size guard fires because `unknown_tx_hashes.len()` remains 25 000.
3. From peer B, send one `RelayTransactionHashes` message with 25 000 *different* fabricated tx hashes. This pushes `unknown_tx_hashes.len()` to 50 000, triggering the guard.
4. The nested loop at lines 1516–1523 of `sync/src/types/mod.rs` now executes ~250 million iterations while holding the `unknown_tx_hashes` mutex, blocking all relay operations on the victim node for the duration.

**Expected outcome:** The victim node's transaction relay is stalled; legitimate peers cannot get their transactions relayed or fetched during the lock-hold period. The attack can be repeated continuously.

### Citations

**File:** sync/src/types/mod.rs (L1256-1261)
```rust
#[derive(Eq, PartialEq, Clone)]
pub struct UnknownTxHashPriority {
    request_time: Instant,
    peers: Vec<PeerIndex>,
    requested: bool,
}
```

**File:** sync/src/types/mod.rs (L1291-1293)
```rust
    pub fn push_peer(&mut self, peer_index: PeerIndex) {
        self.peers.push(peer_index);
    }
```

**File:** sync/src/types/mod.rs (L1490-1495)
```rust
            match unknown_tx_hashes.entry(tx_hash) {
                keyed_priority_queue::Entry::Occupied(entry) => {
                    let mut priority = entry.get_priority().clone();
                    priority.push_peer(peer_index);
                    entry.set_priority(priority);
                }
```

**File:** sync/src/types/mod.rs (L1507-1509)
```rust
        if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
            || unknown_tx_hashes.len()
                >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
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

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L1-1)
```rust
use crate::relayer::{MAX_RELAY_TXS_NUM_PER_BATCH, Relayer};
```
