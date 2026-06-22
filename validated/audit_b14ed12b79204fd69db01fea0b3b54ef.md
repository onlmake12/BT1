### Title
Per-Peer `unknown_tx_hashes` Limit Bypass via Current-State Counting — (`sync/src/types/mod.rs`)

### Summary

`add_ask_for_txs` in `sync/src/types/mod.rs` enforces a per-peer limit on unknown transaction hash announcements by counting the peer's **current** entries in the `unknown_tx_hashes` queue — a mutable, decreasing value — rather than a monotonic per-peer counter. Because entries are permanently removed from the queue after the retry timeout (30 s), a malicious peer can cycle through batches of tx-hash announcements indefinitely, bypassing the intended `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` cap and causing the node to repeatedly issue `GetRelayTransactions` requests for non-existent transactions.

### Finding Description

`add_ask_for_txs` (called when a peer sends a `RelayTransactionHashes` P2P message) works in two phases:

**Phase 1 — unconditional insertion** (lines 1486–1504): up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32 767` hashes from the peer are inserted into `unknown_tx_hashes` with **no per-peer check**. [1](#0-0) 

**Phase 2 — conditional per-peer check** (lines 1506–1529): only when the global queue length reaches `MAX_UNKNOWN_TX_HASHES_SIZE (50 000)` or `peers_count × 32 767` does the code count how many entries in the queue currently belong to this peer, and ban the peer if that count ≥ 32 767. [2](#0-1) 

The per-peer counter is computed by iterating over the live queue: [3](#0-2) 

Entries are **permanently removed** from `unknown_tx_hashes` in two ways:

1. `mark_as_known_txs` removes an entry the moment the corresponding transaction is verified. [4](#0-3) 

2. `pop_ask_for_txs` permanently drops an entry when `next_request_peer()` returns `None` — which happens after the first request attempt when only one peer announced the hash (i.e., after the 30-second `RETRY_ASK_TX_TIMEOUT_INCREASE` window). [5](#0-4) [6](#0-5) 

Because the per-peer count is derived from the **current** queue state (analogous to `balanceOf` in the NFT report), once entries are consumed the count drops to zero and the peer can submit a fresh batch — exactly the same pattern as transferring NFTs away to reset `balanceOf`.

The constants involved: [7](#0-6) 

### Impact Explanation

A single malicious peer can:

1. Send `RelayTransactionHashes` with `N < MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` unique hashes (e.g. 32 766) — below the threshold that triggers the global-limit check with one connected peer.
2. The node issues `GetRelayTransactions` for all those hashes; the peer ignores the requests.
3. After ~30 seconds (`RETRY_ASK_TX_TIMEOUT_INCREASE`), `pop_ask_for_txs` permanently drops all entries (single-peer path in `next_request_peer`).
4. The peer sends another batch of 32 766 fresh hashes. The queue is empty again, so the global-limit check never fires, and the peer is never banned.
5. Repeat indefinitely.

Effect: the victim node continuously sends `GetRelayTransactions` messages for non-existent transactions, wasting outbound bandwidth and CPU. The `unknown_tx_hashes` queue is kept perpetually occupied by the attacker's entries, delaying or crowding out legitimate tx-hash announcements from honest peers. The attacker is never banned because the per-peer count resets each cycle.

### Likelihood Explanation

Any unprivileged peer that can establish a relay connection can execute this attack. No special keys, hashpower, or Sybil capability is required. The rate limiter in `Relayer::try_process` (30 messages/second per peer/message-type) does not prevent the bypass — it only limits message frequency, not the cumulative number of hashes submitted across cycles. [8](#0-7) 

### Recommendation

Replace the current-state scan with a **monotonic per-peer counter** stored in `PeerState`. Increment it on every hash inserted in `add_ask_for_txs` and never decrement it (or decay it only on a long time-window). Reject or ban the peer as soon as its cumulative counter exceeds `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, regardless of whether earlier entries have already been consumed from the queue.

### Proof of Concept

```
// Attacker peer loop (pseudo-code):
loop {
    // Send just below the per-peer limit so the global check never fires
    let hashes = generate_unique_hashes(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER - 1);
    send_relay_tx_hashes(hashes);          // node inserts all, no ban check triggered

    // Node requests the txs; attacker ignores GetRelayTransactions.
    // After RETRY_ASK_TX_TIMEOUT_INCREASE (30 s), pop_ask_for_txs drops all entries
    // because next_request_peer() returns None (single-peer path).
    sleep(31_seconds);

    // unknown_tx_hashes is now empty for this peer → per-peer count = 0
    // → repeat without ever hitting the ban condition
}
```

The node sends `(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER - 1)` × `GetRelayTransactions` requests every ~30 seconds, indefinitely, with no ban ever issued.

### Citations

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

**File:** sync/src/types/mod.rs (L1466-1479)
```rust
        while let Some((tx_hash, mut priority)) = unknown_tx_hashes.pop() {
            if priority.should_request(now) {
                if let Some(peer_index) = priority.next_request_peer() {
                    result
                        .entry(peer_index)
                        .and_modify(|hashes| hashes.push(tx_hash.clone()))
                        .or_insert_with(|| vec![tx_hash.clone()]);
                    unknown_tx_hashes.push(tx_hash, priority);
                }
            } else {
                unknown_tx_hashes.push(tx_hash, priority);
                break;
            }
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

**File:** util/constant/src/sync.rs (L67-72)
```rust
/// The maximum number transaction hashes inside a `RelayTransactionHashes` message
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```
