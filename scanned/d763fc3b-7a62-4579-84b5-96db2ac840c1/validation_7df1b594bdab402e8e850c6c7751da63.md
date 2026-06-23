Now I have all the information needed for a complete analysis. Let me trace every step precisely.

### Title
Intra-Message Duplicate Hash Bypass Inflates `UnknownTxHashPriority::peers` Vec Without Bound Check — (`sync/src/relayer/transaction_hashes_process.rs`, `sync/src/types/mod.rs`)

---

### Summary

An unprivileged remote peer can send a single `RelayTransactionHashes` P2P message containing the same `Byte32` hash repeated up to 32,767 times. Because `execute()` never inserts hashes into `tx_filter` during its filter pass, all duplicates survive the filter and are forwarded to `add_ask_for_txs`. Inside `add_ask_for_txs`, the first occurrence creates a `Vacant` queue entry; every subsequent occurrence hits the `Occupied` branch and calls `push_peer()` unconditionally, inflating the `peers` `Vec` for that single entry to 32,767 identical `PeerIndex` values. The post-insertion overflow guard is keyed on `unknown_tx_hashes.len()` (the count of **unique** hashes), which equals 1 for this attack, so the guard never fires and the attacker is never banned.

---

### Finding Description

**Entrypoint — `execute()` in `transaction_hashes_process.rs`**

The filter pass reads `tx_filter` but never writes to it:

```rust
let tx_hashes: Vec<_> = {
    let mut tx_filter = state.tx_filter();
    tx_filter.remove_expired();
    self.message
        .tx_hashes()
        .iter()
        .map(|x| x.to_entity())
        .filter(|tx_hash| !tx_filter.contains(tx_hash))  // read-only
        .collect()
};
``` [1](#0-0) 

If hash H is absent from `tx_filter`, all K copies of H pass the filter and land in `tx_hashes`. The only upstream guard is the count check (`> MAX_RELAY_TXS_NUM_PER_BATCH`), which allows up to 32,767 entries — all identical. [2](#0-1) 

**State mutation — `add_ask_for_txs()` in `sync/src/types/mod.rs`**

The loop iterates up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (= 32,767) items:

```rust
for tx_hash in tx_hashes.into_iter().take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER) {
    match unknown_tx_hashes.entry(tx_hash) {
        keyed_priority_queue::Entry::Occupied(entry) => {
            let mut priority = entry.get_priority().clone();
            priority.push_peer(peer_index);   // no dedup
            entry.set_priority(priority);
        }
        keyed_priority_queue::Entry::Vacant(entry) => { ... }
    }
}
``` [3](#0-2) 

`push_peer` is unconditional:

```rust
pub fn push_peer(&mut self, peer_index: PeerIndex) {
    self.peers.push(peer_index);
}
``` [4](#0-3) 

With K = 32,767 copies of hash H: copy 1 → `Vacant` (creates entry, `peers = [P]`); copies 2–32,767 → `Occupied` → `push_peer` called 32,766 times → `peers = [P, P, P, …]` (32,767 entries).

**Why the overflow guard is bypassed**

The post-insertion check fires only when the number of **unique** hashes in the queue reaches `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000):

```rust
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
    || unknown_tx_hashes.len() >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
{
    // peer_unknown_counter check lives here
}
``` [5](#0-4) 

With all-same-hash input, `unknown_tx_hashes.len()` = 1 after the loop. The condition is false, the `peer_unknown_counter` scan is never reached, and `Status::ok()` is returned. The attacker is not banned. [6](#0-5) 

**Downstream effect — `pop_ask_for_txs` / `next_request_peer`**

`next_request_peer` drains the `peers` Vec one entry per call via `swap_remove(0)`:

```rust
pub fn next_request_peer(&mut self) -> Option<PeerIndex> {
    if self.requested {
        if self.peers.len() > 1 {
            self.request_time = Instant::now();
            self.peers.swap_remove(0);
            self.peers.first().cloned()
        } else { None }
    } else { ... }
}
``` [7](#0-6) 

With 32,767 identical peer entries, the entry stays live in `unknown_tx_hashes` for 32,767 `pop_ask_for_txs` cycles, each emitting a `GetRelayTransactions` message to the same peer for the same hash.

---

### Impact Explanation

| Resource | Per-message cost | Sustained (30 msg/s rate-limit ceiling) |
|---|---|---|
| Memory (`peers` Vec) | 32,767 × sizeof(PeerIndex) ≈ 128 KB | Grows with unique hashes sent |
| Outbound network | 32,767 `GetRelayTransactions` per hash | Amplified per additional unique hash |
| CPU (`pop_ask_for_txs`) | 32,767 extra iterations per hash | Proportional |

A single well-crafted message causes immediate, bounded memory inflation and a prolonged outbound request storm to the attacker's address for a hash the attacker will never fulfill. The rate limiter (30 msg/s per peer/message-type) does not prevent this because the damage is packed into one message.

---

### Likelihood Explanation

The attack requires only a standard P2P connection — no keys, no PoW, no privileged role. The message format is valid (count ≤ 32,767). The attacker constructs a `RelayTransactionHashes` flatbuffer with one hash repeated 32,767 times. This is trivially scriptable and reproducible in a local integration test.

---

### Recommendation

Deduplicate `tx_hashes` before calling `add_ask_for_txs`, either in `execute()` (insert into a `HashSet` during the filter pass) or at the top of `add_ask_for_txs`. Additionally, `push_peer` should guard against duplicate `PeerIndex` entries within the same `UnknownTxHashPriority`.

---

### Proof of Concept

```rust
// In execute(): replace the filter+collect with a deduplicating version
let tx_hashes: Vec<_> = {
    let mut tx_filter = state.tx_filter();
    tx_filter.remove_expired();
    let mut seen = std::collections::HashSet::new();
    self.message
        .tx_hashes()
        .iter()
        .map(|x| x.to_entity())
        .filter(|tx_hash| !tx_filter.contains(tx_hash) && seen.insert(tx_hash.clone()))
        .collect()
};
// After this fix, 32767 copies of H → seen rejects copies 2-32767 → tx_hashes.len() == 1
```

Unit test assertion (before fix): send message with 1,000 copies of hash H; after `add_ask_for_txs`, assert `unknown_tx_hashes[H].peers.len() == 1`. Without the fix this assertion fails (len = 1,000).

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
