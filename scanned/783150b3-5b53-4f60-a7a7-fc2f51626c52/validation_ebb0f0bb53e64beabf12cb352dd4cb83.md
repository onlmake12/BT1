### Title
Unbounded `peers` Vec Growth via Duplicate Hashes in `RelayTransactionHashes` — (`sync/src/relayer/transaction_hashes_process.rs`)

---

### Summary

An unprivileged remote peer can send `RelayTransactionHashes` messages containing up to 32,767 identical hash values. Because `execute()` performs no intra-message deduplication and `add_ask_for_txs` has no bound on the `peers` Vec inside `UnknownTxHashPriority`, each duplicate hash causes an unconditional `push_peer()` call. Repeated messages grow the `peers` Vec without limit, enabling memory exhaustion on the target node.

---

### Finding Description

**Step 1 — Entry point and count check.**

`execute()` rejects messages only when `len > MAX_RELAY_TXS_NUM_PER_BATCH`, so a message with exactly 32,767 hashes passes: [1](#0-0) 

**Step 2 — `tx_filter` does not deduplicate within a message.**

The filter only removes hashes that are already *known* (previously verified transactions). It does not deduplicate repeated occurrences of the same hash within the current message: [2](#0-1) 

A fresh, never-seen hash passes through all 32,767 times.

**Step 3 — `add_ask_for_txs` processes all 32,767 items.**

The `.take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)` limit equals 32,767, so nothing is dropped: [3](#0-2) 

For the first occurrence the `Vacant` branch creates the entry with `peers: vec![peer_index]`. Every subsequent occurrence (32,766 more) hits the `Occupied` branch and calls `push_peer(peer_index)` unconditionally.

**Step 4 — `push_peer` has no bound.** [4](#0-3) 

**Step 5 — The post-insert overflow check is blind to `peers` Vec length.**

The guard checks `unknown_tx_hashes.len()` (number of *distinct* hashes in the map). With 32,767 identical hashes only one distinct entry exists, so `len == 1`, far below `MAX_UNKNOWN_TX_HASHES_SIZE = 50000`: [5](#0-4) 

The check never fires.

**Step 6 — Drain rate is one entry per 30-second cycle.**

`next_request_peer()` removes at most one peer per scheduling cycle via `swap_remove(0)`: [6](#0-5) 

An attacker sending one message per second adds 32,767 entries per second while the node drains one every 30 seconds — a net growth rate of ~32,766 `PeerIndex` values per second per attacking peer.

**Constants for reference:** [7](#0-6) 

---

### Impact Explanation

Each `PeerIndex` is a small integer (typically 4–8 bytes). At 32,767 entries per message and one message per second, a single peer can force the node to allocate ~130 KiB/s into a single `Vec` that is never reclaimed until the hash is resolved. With multiple peers or higher message rates the growth is proportional. This leads to unbounded heap growth and eventual OOM termination of the node — a remote denial-of-service with no authentication required.

---

### Likelihood Explanation

The exploit requires only a standard P2P connection. No PoW, no keys, no privileged role. The attacker needs to know one hash that is not in the node's `tx_filter` (trivially satisfied by any random 32-byte value). The attack is repeatable indefinitely because the hash never enters `tx_filter` unless the corresponding transaction is actually verified.

---

### Recommendation

Apply **both** fixes:

1. **Deduplicate within `execute()`** before passing hashes to `add_ask_for_txs`:
   ```rust
   let tx_hashes: Vec<_> = { ... }.into_iter().collect::<HashSet<_>>().into_iter().collect();
   ```

2. **Cap the `peers` Vec in `push_peer` or in `add_ask_for_txs`** — e.g., refuse to append if `peers.len()` already exceeds a small constant (e.g., 8 or the number of connected peers):
   ```rust
   pub fn push_peer(&mut self, peer_index: PeerIndex) {
       if self.peers.len() < MAX_PEERS_PER_UNKNOWN_TX {
           self.peers.push(peer_index);
       }
   }
   ```

---

### Proof of Concept

```rust
// Construct a RelayTransactionHashes with 32767 copies of the same hash
let fake_hash = Byte32::new([0xAB; 32]);
let hashes: Vec<Byte32> = std::iter::repeat(fake_hash.clone()).take(32767).collect();
let msg = packed::RelayTransactionHashes::new_builder()
    .tx_hashes(hashes.pack())
    .build();

// Call execute() — passes the count check (32767 == MAX, not >)
// tx_filter does not contain fake_hash, so all 32767 pass the filter
// add_ask_for_txs: first hash → Vacant, hashes 2–32767 → Occupied → push_peer() x32766

let state = relayer.shared().state();
state.add_ask_for_txs(peer_index, vec![fake_hash.clone(); 32767]);

let guard = state.unknown_tx_hashes();
let priority = guard.get(&fake_hash).unwrap();
assert_eq!(priority.peers.len(), 32767); // invariant violated

// Repeat the message every second → peers.len() grows by ~32767 per iteration
// with no upper bound enforced anywhere in the stack
```

### Citations

**File:** sync/src/relayer/transaction_hashes_process.rs (L29-35)
```rust
            if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    relay_transaction_hashes.tx_hashes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L38-47)
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
```

**File:** sync/src/types/mod.rs (L1276-1289)
```rust
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
```

**File:** sync/src/types/mod.rs (L1291-1293)
```rust
    pub fn push_peer(&mut self, peer_index: PeerIndex) {
        self.peers.push(peer_index);
    }
```

**File:** sync/src/types/mod.rs (L1486-1504)
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
        }
```

**File:** sync/src/types/mod.rs (L1507-1529)
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
