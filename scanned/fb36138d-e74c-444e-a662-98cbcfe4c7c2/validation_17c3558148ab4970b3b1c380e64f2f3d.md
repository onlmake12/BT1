### Title
Unbounded `peers` Vec Growth in `UnknownTxHashPriority` via Repeated `RelayTransactionHashes` — (`sync/src/types/mod.rs`)

---

### Summary

A malicious P2P peer can cause unbounded memory growth on a CKB node by repeatedly sending `RelayTransactionHashes` messages containing the same set of transaction hashes. Each repeated announcement appends the sender's `PeerIndex` to the `peers` `Vec` inside `UnknownTxHashPriority` without deduplication or a per-entry size cap. The post-insertion guard that enforces a per-peer limit only fires when the total number of *distinct* hashes in `unknown_tx_hashes` exceeds `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000). Because repeated sends of the same hashes do not increase the map's key count, that guard is never reached, and the `peers` `Vec` for each entry grows without bound.

---

### Finding Description

`SyncState::add_ask_for_txs` in `sync/src/types/mod.rs` handles incoming `RelayTransactionHashes` announcements from peers. [1](#0-0) 

For each announced hash that already exists in the `unknown_tx_hashes` keyed-priority-queue, the code calls `priority.push_peer(peer_index)`, which appends the peer's index to the `peers` `Vec` inside `UnknownTxHashPriority`: [2](#0-1) 

There is no deduplication of `peer_index` within the `peers` `Vec`, and no cap on how large that `Vec` may grow. The only guard present is a post-insertion check: [3](#0-2) 

This guard fires only when `unknown_tx_hashes.len()` (the count of *distinct* hash keys) reaches `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000) or `peers.state.len() × MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`. Because repeated sends of the **same** hashes do not add new keys to the map, the map's key count stays constant and the guard is never triggered. The `peers` `Vec` inside every affected entry grows by one entry per repeated call.

The relevant constants are: [4](#0-3) 

`MAX_RELAY_TXS_NUM_PER_BATCH` = 32,767 hashes per message; `MAX_UNKNOWN_TX_HASHES_SIZE` = 50,000 distinct keys; `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` = 32,767.

---

### Impact Explanation

**Memory exhaustion (OOM / node crash).** Each `PeerIndex` is a `usize` (8 bytes). With 32,767 hashes per message and N repeated sends, the node allocates approximately `32,767 × N × 8` bytes just for the `peers` `Vec` entries. At N = 10,000 sends the footprint exceeds 2.6 GB. A single malicious peer can drive the node to OOM, causing a crash or severe degradation that affects all connected peers and halts block/transaction relay.

---

### Likelihood Explanation

The attack requires only a standard P2P connection — no privileged role, no key material, no majority hashpower. The attacker sends the same `RelayTransactionHashes` payload in a tight loop. The protocol imposes no rate limit on this message type beyond the per-call `take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)` truncation, which does not prevent repeated calls. The attack is cheap for the attacker (small, fixed-size messages) and expensive for the victim (unbounded memory allocation).

---

### Recommendation

1. **Deduplicate `peer_index` inside `push_peer`**: use a `HashSet<PeerIndex>` instead of `Vec<PeerIndex>` for the `peers` field of `UnknownTxHashPriority`, or check for existence before pushing.
2. **Cap the `peers` collection per entry**: enforce a maximum number of peers that can be recorded per unknown tx hash.
3. **Move the per-peer limit check to a pre-insertion guard**: count the peer's existing entries *before* inserting, not only when the global map size threshold is crossed.

---

### Proof of Concept

```
1. Attacker peer connects to a CKB node via the Relay protocol.
2. Attacker constructs a RelayTransactionHashes message containing
   32,767 transaction hashes that are unknown to the node.
3. Attacker sends the same message in a tight loop (N iterations).
4. After each send, add_ask_for_txs is called:
   - unknown_tx_hashes.len() stays at 32,767 (below MAX_UNKNOWN_TX_HASHES_SIZE=50,000)
   - The post-insertion guard at line 1507 never fires.
   - push_peer(attacker_peer) appends attacker's PeerIndex to each of
     the 32,767 entries' peers Vec.
5. After N=10,000 iterations:
   - Each of the 32,767 entries holds a peers Vec of length 10,000.
   - Total memory for peers Vecs alone: 32,767 × 10,000 × 8 bytes ≈ 2.6 GB.
6. Node runs out of memory and crashes or becomes unresponsive.
```

### Citations

**File:** sync/src/types/mod.rs (L1483-1504)
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
```

**File:** sync/src/types/mod.rs (L1506-1529)
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
