### Title
Unbounded O(N×M) Linear Scan in `add_ask_for_txs` Triggered by Unprivileged Relay Peers — (`File: sync/src/types/mod.rs`)

---

### Summary

`SyncState::add_ask_for_txs` in `sync/src/types/mod.rs` performs a full O(N×M) linear scan over the entire `unknown_tx_hashes` priority queue — up to 50,000 entries, each with an unbounded inner `peers` Vec — every time the queue is at capacity and a peer sends a `RelayTransactionHashes` message. Any unprivileged peer can fill the queue to capacity and then repeatedly trigger this expensive scan while holding the shared `unknown_tx_hashes` mutex, causing sustained CPU exhaustion and lock contention that degrades relay and block-propagation throughput.

---

### Finding Description

**Root cause — the O(N×M) scan:**

In `add_ask_for_txs`, after inserting new hashes, the code checks whether `unknown_tx_hashes` has exceeded its soft cap. When it has, it iterates over every entry in the queue and every peer inside each entry's `peers` Vec to count how many entries belong to the calling peer:

```rust
// sync/src/types/mod.rs  lines 1506-1528
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE          // 50 000
    || unknown_tx_hashes.len()
        >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
{
    let mut peer_unknown_counter = 0;
    for (_hash, priority) in unknown_tx_hashes.iter() {   // O(N)
        for peer in priority.peers.iter() {               // O(M) per entry
            if *peer == peer_index {
                peer_unknown_counter += 1;
            }
        }
    }
    ...
}
```

The entire scan executes while holding the `unknown_tx_hashes` `Mutex` lock (acquired at line 1484), blocking every other caller that needs the lock.

**Root cause — unbounded `peers` Vec per entry:**

`UnknownTxHashPriority::push_peer` appends without any cap:

```rust
// sync/src/types/mod.rs  lines 1291-1293
pub fn push_peer(&mut self, peer_index: PeerIndex) {
    self.peers.push(peer_index);
}
```

When multiple peers announce the same tx hash, the `Occupied` branch calls `push_peer` for each, growing the inner `peers` Vec to the number of announcing peers. With P connected peers all announcing the same hash, that single entry's inner loop costs O(P) per scan iteration.

**Attack path:**

1. Attacker connects multiple peers (inbound connections are accepted by default).
2. Each peer sends `RelayTransactionHashes` messages with up to `MAX_RELAY_TXS_NUM_PER_BATCH` = 32,767 unique tx hashes per message.
3. Two peers each sending ~25,000 unique hashes fills `unknown_tx_hashes` to `MAX_UNKNOWN_TX_HASHES_SIZE` = 50,000.
4. From this point, every subsequent `RelayTransactionHashes` message from any peer triggers the full O(N×M) scan.
5. The rate limiter (`Relayer::new`, `sync/src/relayer/mod.rs` line 91) allows 30 messages/second per `(peer, message_type)` pair. With K attacker-controlled peers, the scan fires up to 30×K times per second.
6. If all K peers also announce the same hashes, each entry's `peers` Vec grows to K, making each scan O(50,000 × K).

**Call chain:**

```
P2P RelayTransactionHashes message
  → Relayer::try_process  (sync/src/relayer/mod.rs:143)
  → TransactionHashesProcess::execute  (sync/src/relayer/transaction_hashes_process.rs:49)
  → SyncState::add_ask_for_txs  (sync/src/types/mod.rs:1483)
      → mutex lock acquired
      → O(N×M) scan  (lines 1517-1523)
      → mutex released
```

---

### Impact Explanation

- **CPU exhaustion**: At 50,000 entries × K peers per entry × 30K scans/second, the work scales quadratically with the number of attacker peers. Even with 10 peers each announcing distinct hashes, the scan fires 300 times/second over 50,000 entries — tens of millions of comparisons per second on the relay thread.
- **Mutex starvation**: The `unknown_tx_hashes` lock is held for the entire scan duration. Other relay operations that need this lock (`mark_as_known_txs`, `pop_ask_for_txs`, `tx_filter`) are blocked, degrading transaction relay and compact-block reconstruction.
- **Block propagation degradation**: Sustained lock contention on the relay state can delay compact-block processing, harming the node's ability to propagate and receive blocks in a timely manner.

Severity: **High** — cheap to mount (only requires connecting peers and sending valid P2P messages), causes measurable service degradation with no privileged access.

---

### Likelihood Explanation

- Any peer can connect to a CKB node without authentication.
- `RelayTransactionHashes` is a standard relay protocol message; sending it with fabricated (non-existent) tx hashes is trivially cheap — no PoW, no fee, no valid UTXO required.
- Filling `unknown_tx_hashes` to 50,000 entries requires only two peers each sending one batch of ~25,000 hashes.
- The rate limiter (30/s per peer) does not prevent the attack; it only bounds the per-peer rate, not the aggregate rate across many peers.
- The existing `TooManyUnknownTransactions` test (`test/src/specs/relay/too_many_unknown_transactions.rs`) confirms the code path is reachable and exercised.

---

### Recommendation

1. **Replace the O(N×M) scan with a per-peer counter map.** Maintain a `HashMap<PeerIndex, usize>` alongside `unknown_tx_hashes` that is updated incrementally on insert/remove, making the overflow check O(1).
2. **Cap the `peers` Vec per entry.** In `push_peer`, enforce a maximum number of peers per `UnknownTxHashPriority` entry (e.g., equal to the maximum number of connected peers) to bound the inner loop.
3. **Enforce a hard cap, not a soft cap.** The current check is described as a "soft limit" (`MAX_UNKNOWN_TX_HASHES_SIZE`). Reject insertions before they occur rather than scanning after.

---

### Proof of Concept

```
1. Attacker opens K=10 inbound connections to the target node.

2. Each peer sends one RelayTransactionHashes message containing
   5,000 unique, fabricated tx hashes (total: 50,000 entries fill
   unknown_tx_hashes to MAX_UNKNOWN_TX_HASHES_SIZE).

3. All K peers also announce the same 1,000 shared hashes, causing
   push_peer to be called K times per shared entry → peers Vec length = K.

4. Each peer now sends RelayTransactionHashes at 30 msg/s (rate limit).
   → 300 scans/second, each iterating 50,000 entries × K peers = 500,000
     comparisons per scan → 150,000,000 comparisons/second.

5. The unknown_tx_hashes mutex is held for each scan, starving
   mark_as_known_txs, pop_ask_for_txs, and tx_filter operations.

6. Observable effect: relay latency spikes, compact-block reconstruction
   stalls, CPU pegged on the relay async task.
```

**Key lines:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** sync/src/types/mod.rs (L1291-1293)
```rust
    pub fn push_peer(&mut self, peer_index: PeerIndex) {
        self.peers.push(peer_index);
    }
```

**File:** sync/src/types/mod.rs (L1506-1528)
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
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L38-50)
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
