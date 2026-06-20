### Title
Unbounded Linear Scan Over `unknown_tx_hashes` on Every Peer Message Causes CPU/Lock DoS — (`File: sync/src/types/mod.rs`)

---

### Summary

In `SyncState::add_ask_for_txs`, when the `unknown_tx_hashes` priority queue reaches its capacity limit, every subsequent `RelayTransactionHashes` P2P message from any peer triggers a full O(N×M) nested scan over all queued hashes and all peer lists within them — while holding the queue's mutex lock. This is a direct analog of the "push pattern" vulnerability: instead of maintaining a per-peer counter (pull pattern), the code performs a full linear traversal of the entire shared collection on every inbound message, enabling any unprivileged peer to cause sustained CPU exhaustion and mutex starvation in the relay subsystem.

---

### Finding Description

In `sync/src/types/mod.rs`, the function `add_ask_for_txs` is called every time a peer sends a `RelayTransactionHashes` P2P message. After inserting the new hashes, the function checks whether the global `unknown_tx_hashes` queue has reached its size limit. If it has, it performs a nested loop:

```rust
// sync/src/types/mod.rs lines 1516–1523
let mut peer_unknown_counter = 0;
for (_hash, priority) in unknown_tx_hashes.iter() {
    for peer in priority.peers.iter() {
        if *peer == peer_index {
            peer_unknown_counter += 1;
        }
    }
}
```

This scan iterates over every entry in `unknown_tx_hashes` (up to `MAX_UNKNOWN_TX_HASHES_SIZE`) and for each entry, over every peer index stored in that entry's `peers` vector. The entire scan is performed while holding the `unknown_tx_hashes` mutex lock (acquired at line 1484 via `self.unknown_tx_hashes.lock()`).

The trigger path is:
1. A peer sends `RelayTransactionHashes` → `TransactionHashesProcess::execute()` in `sync/src/relayer/transaction_hashes_process.rs` (line 49) calls `state.add_ask_for_txs(self.peer, tx_hashes)`.
2. `add_ask_for_txs` inserts hashes up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` per peer (line 1488).
3. Once `unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE` (line 1507), every subsequent call from any peer triggers the full nested scan.
4. The scan holds the mutex for its entire duration, blocking all concurrent callers of `add_ask_for_txs`, `pop_ask_for_txs`, and `mark_as_known_txs`.

The check at line 1029 in `transaction_hashes_process.rs` only bounds the per-message count (`MAX_RELAY_TXS_NUM_PER_BATCH`), not the frequency of messages. An attacker can send messages at the maximum allowed rate continuously.

---

### Impact Explanation

- **CPU exhaustion**: The O(N×M) scan (N = `MAX_UNKNOWN_TX_HASHES_SIZE` entries, M = peers per entry) runs on every inbound `RelayTransactionHashes` message once the queue is saturated. Multiple peers sending at maximum rate multiply the CPU cost.
- **Mutex starvation**: The `unknown_tx_hashes` lock is held for the entire scan duration. All relay subsystem operations that depend on this lock — including `pop_ask_for_txs` (used by `ask_for_txs` in `sync/src/relayer/mod.rs` line 606) and `mark_as_known_txs` (line 1443) — are blocked, stalling transaction relay for all peers.
- **Transaction relay DoS**: The relay subsystem's ability to request and propagate transactions is degraded or halted, preventing the node from participating in normal mempool gossip.

---

### Likelihood Explanation

- The queue saturation condition (`unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE`) is reachable by any set of peers sending `RelayTransactionHashes` messages with unknown tx hashes. Once saturated, the condition persists as long as peers keep sending new unknown hashes.
- The `RelayTransactionHashes` message is a standard, unauthenticated P2P relay message reachable by any connected peer without any privileged role.
- The per-message limit (`MAX_RELAY_TXS_NUM_PER_BATCH`) bounds individual message size but not message frequency, so a single peer can continuously trigger the scan.

---

### Recommendation

Replace the full linear scan with a per-peer counter maintained as a separate data structure (pull pattern). Specifically:

- Maintain a `HashMap<PeerIndex, usize>` (or `DashMap`) that tracks how many entries in `unknown_tx_hashes` each peer is associated with.
- Increment/decrement this counter when entries are added or removed.
- In `add_ask_for_txs`, replace the nested loop with a single O(1) lookup into this per-peer counter map.

This eliminates the O(N×M) scan entirely and removes the extended mutex hold time.

---

### Proof of Concept

**Entry point**: Any connected peer sends repeated `RelayTransactionHashes` P2P messages containing hashes not present in the local tx filter.

**Step 1**: Peer(s) send enough `RelayTransactionHashes` messages to fill `unknown_tx_hashes` to `MAX_UNKNOWN_TX_HASHES_SIZE`.

**Step 2**: Once saturated, every subsequent `RelayTransactionHashes` from any peer triggers: [1](#0-0) 

The nested loop at lines 1517–1523 scans all N entries and all M peer indices per entry while holding the mutex acquired at: [2](#0-1) 

**Step 3**: The inbound message handler that triggers this is: [3](#0-2) 

The per-message count check at line 29 only limits hashes per message, not call frequency.

**Step 4**: The mutex stall blocks `pop_ask_for_txs` (called in the relay loop): [4](#0-3) 

and `mark_as_known_txs`: [5](#0-4) 

halting transaction relay for all peers on the node.

### Citations

**File:** sync/src/types/mod.rs (L1443-1451)
```rust
    pub fn mark_as_known_txs(&self, hashes: impl Iterator<Item = Byte32> + std::clone::Clone) {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
        let mut tx_filter = self.tx_filter.lock();

        for hash in hashes {
            unknown_tx_hashes.remove(&hash);
            tx_filter.insert(hash);
        }
    }
```

**File:** sync/src/types/mod.rs (L1484-1484)
```rust
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
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

**File:** sync/src/relayer/mod.rs (L606-628)
```rust
        for (peer, mut tx_hashes) in self.shared().state().pop_ask_for_txs() {
            if !tx_hashes.is_empty() {
                debug_target!(
                    crate::LOG_TARGET_RELAY,
                    "Send get transaction ({} hashes) to {}",
                    tx_hashes.len(),
                    peer,
                );
                tx_hashes.truncate(MAX_RELAY_TXS_NUM_PER_BATCH);
                let content = packed::GetRelayTransactions::new_builder()
                    .tx_hashes(tx_hashes)
                    .build();
                let message = packed::RelayMessage::new_builder().set(content).build();
                let status = async_send_message_to(nc, peer, &message).await;
                if !status.is_ok() {
                    ckb_logger::error!(
                        "interrupted request for transactions, status: {:?}",
                        status
                    );
                }
            }
        }
    }
```
