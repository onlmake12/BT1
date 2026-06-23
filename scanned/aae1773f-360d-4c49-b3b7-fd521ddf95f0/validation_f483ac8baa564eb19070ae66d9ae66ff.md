### Title
Global `unknown_tx_hashes` Pool Exhaustion Allows Attacker to Silence Legitimate Peers' Transaction Announcements — (`sync/src/types/mod.rs`)

---

### Summary

The `add_ask_for_txs` function in `sync/src/types/mod.rs` maintains a global `unknown_tx_hashes` pool bounded by `MAX_UNKNOWN_TX_HASHES_SIZE` (50 000 entries). The per-peer ban check fires only when a **single** peer contributes ≥ `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32 767) entries. An attacker operating two cheap peers can collectively fill the global pool above 50 000 entries without either peer being banned. Once the pool is full, every subsequent `RelayTransactionHashes` message from any legitimate peer returns `Status::ignored()` — the node silently discards those transaction hash announcements and never requests the corresponding transactions, stalling transaction propagation.

---

### Finding Description

`add_ask_for_txs` is called whenever a peer sends a `RelayTransactionHashes` P2P message. Its logic is:

1. **Insert first, check later.** All hashes in the incoming batch (up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` = 32 767) are unconditionally inserted into the shared `unknown_tx_hashes` keyed-priority-queue before any limit is evaluated. [1](#0-0) 

2. **Post-insertion global check.** After insertion the function checks whether the pool has reached the global soft limit. [2](#0-1) 

3. **Per-peer ban threshold.** Only if the *current* peer alone has contributed ≥ 32 767 entries is it banned (`TooManyUnknownTransactions`). Otherwise the function returns `Status::ignored()`. [3](#0-2) 

4. **No eviction on peer disconnect.** There is no code path that removes entries from `unknown_tx_hashes` when a peer disconnects. Entries contributed by a disconnected attacker peer persist indefinitely until they are individually requested and expire.

The constants that define the exploitable gap: [4](#0-3) 

**Attack sequence (two attacker peers, zero cost beyond minimal connectivity):**

| Step | Action | Global pool size | Outcome |
|------|--------|-----------------|---------|
| 1 | Attacker peer A sends 32 767 unique tx hashes | 32 767 | Below 50 000 → `Status::ok()`, peer A not banned |
| 2 | Attacker peer B sends 32 767 unique tx hashes | 65 534 | ≥ 50 000 → peer B count = 32 767 ≥ 32 767 → peer B **banned** |
| 3 | Peer A disconnects (optional) | 65 534 | Entries remain; pool stays saturated |
| 4 | Any legitimate peer sends tx hashes | 65 534 | ≥ 50 000, legitimate peer count = 0 < 32 767 → `Status::ignored()` |

From step 4 onward, every `RelayTransactionHashes` message from every honest peer is silently dropped. The node never issues `GetRelayTransactions` for those hashes, so the transactions are never fetched.

---

### Impact Explanation

Transaction propagation to the victim node is permanently stalled until the `unknown_tx_hashes` pool drains naturally (entries are only removed when the node actually requests and receives the corresponding transactions, which it will never do for the attacker's fake hashes). The victim node's mempool stops receiving new unconfirmed transactions from the P2P network, degrading its ability to mine competitive blocks and breaking RPC clients that rely on seeing pending transactions. This is a griefing DOS with no profit motive required — the attacker only needs two ephemeral P2P connections and 65 534 arbitrary 32-byte hashes.

---

### Likelihood Explanation

Any unprivileged external peer can open two connections to a CKB node (the default outbound/inbound peer limits are generous). Sending `RelayTransactionHashes` with fabricated hashes is a valid, unauthenticated P2P operation. The cost is two TCP connections and two small messages. The attack is repeatable: if the pool drains (e.g., the node restarts), the attacker simply reconnects and repeats. No special knowledge, keys, or majority hashpower is required.

---

### Recommendation

Apply per-peer accounting **before** insertion, not after. Reject or truncate a batch from a peer whose running total would exceed `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` before any entries are written to the shared pool. Additionally, evict all entries attributed to a peer when that peer disconnects, so a disconnected attacker cannot leave the pool permanently saturated. A secondary defence is to enforce a hard per-peer cap inside the insertion loop rather than relying solely on the post-insertion soft-limit check.

---

### Proof of Concept

```
// Attacker peer A: connect to victim node, send RelayTransactionHashes
//   with 32_767 unique fabricated tx hashes (each 32 random bytes).
//   → add_ask_for_txs inserts all 32_767 entries; global pool = 32_767 < 50_000 → Status::ok()
//   → Peer A is NOT banned.

// Attacker peer B: connect to victim node, send RelayTransactionHashes
//   with another 32_767 unique fabricated tx hashes.
//   → add_ask_for_txs inserts all 32_767 entries; global pool = 65_534 ≥ 50_000
//   → peer_unknown_counter for peer B = 32_767 ≥ MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
//   → StatusCode::TooManyUnknownTransactions → peer B banned.
//   → Pool remains at 65_534 entries (peer B's entries are NOT removed on ban).

// Peer A may now disconnect; its 32_767 entries remain in the pool.

// Legitimate peer C: connect to victim node, send RelayTransactionHashes
//   with real mempool tx hashes.
//   → add_ask_for_txs: global pool = 65_534 ≥ 50_000
//   → peer_unknown_counter for peer C = 0 < 32_767
//   → returns Status::ignored()   ← tx hashes silently discarded
//   → victim node never requests those transactions from peer C.
```

Relevant constants and code path: [4](#0-3) [5](#0-4)

### Citations

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

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
