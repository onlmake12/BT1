### Title
Unbounded `peers` Vec Growth in `UnknownTxHashPriority::push_peer` via Repeated `RelayTransactionHashes` — (`sync/src/types/mod.rs`)

---

### Summary

`push_peer` appends a `PeerIndex` to `UnknownTxHashPriority::peers` with no deduplication and no per-entry size cap. The overflow guard in `add_ask_for_txs` only checks the number of **unique hash entries** in the queue, not the size of any individual entry's `peers` Vec. A single rate-limited peer can therefore grow the `peers` Vec for any unresolved hash at 30 entries/second indefinitely, causing unbounded heap growth on the victim node.

---

### Finding Description

**`UnknownTxHashPriority::push_peer`** unconditionally appends without any bound: [1](#0-0) 

**`add_ask_for_txs`** calls `push_peer` in the Occupied branch every time the same hash arrives from any peer: [2](#0-1) 

The overflow guard that follows only fires when `unknown_tx_hashes.len()` (the count of **distinct** hash keys) crosses `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000) or the peer-count threshold: [3](#0-2) 

If only **one** unique hash is in the queue, `unknown_tx_hashes.len()` = 1 — the guard never fires, and the `peers` Vec for that single entry is completely unbounded.

The per-peer counter inside the guard counts how many times `peer_index` appears across all hash entries: [4](#0-3) 

This counter also does not protect against the attack: a single peer sending the same hash 30 times/sec accumulates 30 appearances per second in that one entry, but the counter only triggers at `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` = 32,767 — reachable in ~18 minutes at 30 req/sec, after which the peer is rejected for **that call only**. The Vec already holds 32,767 entries at that point, and the check is not applied on subsequent calls once the queue length drops back below the threshold.

**Consumption rate vs. addition rate:**

`next_request_peer` removes one entry from `peers` via `swap_remove(0)` at most once per `RETRY_ASK_TX_TIMEOUT_INCREASE` (30 seconds): [5](#0-4) 

The rate limiter allows 30 `RelayTransactionHashes` messages/sec per peer: [6](#0-5) 

Addition rate: **30 entries/sec**. Consumption rate: **1 entry/30 sec**. Net growth: ~30 entries/sec per hash per peer.

The `tx_filter` only removes **known** (resolved) hashes: [7](#0-6) 

A hash for a non-existent or permanently-unresolved transaction is never added to `tx_filter`, so it passes the filter on every message and `push_peer` is called every time.

---

### Impact Explanation

- **Memory exhaustion**: A single peer sending 30 `RelayTransactionHashes`/sec with the same unknown hash grows the `peers` Vec at ~30 × 8 bytes = 240 bytes/sec. With `MAX_RELAY_TXS_NUM_PER_BATCH` = 32,767 distinct hashes per message, a single message can create 32,767 queue entries each with a growing `peers` Vec. Over hours, this causes gigabytes of heap growth.
- **Relay scheduling degradation**: `pop_ask_for_txs` iterates the entire queue and calls `next_request_peer` on each entry. With large `peers` Vecs, `swap_remove` is O(1) but the sheer volume of entries processed per scheduling tick increases lock-hold time on `unknown_tx_hashes`, blocking all relay scheduling.
- **No crash recovery**: The `peers` Vec is heap-allocated inside a `Mutex`-protected `KeyedPriorityQueue`. OOM kills the node process.

---

### Likelihood Explanation

The attack requires only a single connected peer sending valid (well-formed) `RelayTransactionHashes` P2P messages at the protocol-permitted rate of 30/sec. No PoW, no keys, no Sybil network required. The hash need not correspond to any real transaction. The path is:

```
peer sends RelayTransactionHashes(same_hash)
  → TransactionHashesProcess::execute          [transaction_hashes_process.rs:25]
  → tx_filter check passes (hash unknown)
  → add_ask_for_txs(peer_index, [same_hash])   [types/mod.rs:1483]
  → Occupied branch → push_peer(peer_index)    [types/mod.rs:1291]
  → peers Vec grows by 1, no bound enforced
``` [8](#0-7) 

---

### Recommendation

1. **Deduplicate in `push_peer`**: Check `self.peers.contains(&peer_index)` before pushing, or use a `HashSet<PeerIndex>` instead of `Vec<PeerIndex>`.
2. **Cap `peers` Vec size**: Enforce a hard limit (e.g., `MAX_RELAY_PEERS` = 128) inside `push_peer` or in the Occupied branch of `add_ask_for_txs`.
3. **Move the overflow check before insertion**: The current guard fires *after* `push_peer` has already grown the Vec. The per-entry peer-list size should be checked before appending.

---

### Proof of Concept

```rust
// Pseudocode — call add_ask_for_txs with the same hash from one peer 32768 times
let state = SyncState::new(...);
let hash = Byte32::zero();
let peer = PeerIndex::new(1);

// First call: Vacant branch, creates entry with peers = [peer]
state.add_ask_for_txs(peer, vec![hash.clone()]);

// Subsequent calls: Occupied branch, push_peer appends unconditionally
for _ in 0..32767 {
    state.add_ask_for_txs(peer, vec![hash.clone()]);
}

// unknown_tx_hashes.len() == 1 (one unique hash)
// overflow guard never fires (1 < 50000)
// peers Vec inside the single entry now has 32768 elements
let guard = state.unknown_tx_hashes.lock();
let priority = guard.get_priority(&hash).unwrap();
assert!(priority.peers.len() == 32768); // unbounded growth confirmed
```

### Citations

**File:** sync/src/types/mod.rs (L1276-1284)
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
```

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

**File:** sync/src/types/mod.rs (L1516-1524)
```rust
            let mut peer_unknown_counter = 0;
            for (_hash, priority) in unknown_tx_hashes.iter() {
                for peer in priority.peers.iter() {
                    if *peer == peer_index {
                        peer_unknown_counter += 1;
                    }
                }
            }
            if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
```

**File:** sync/src/relayer/mod.rs (L91-92)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
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
