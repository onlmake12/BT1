### Title
Per-Peer `unknown_tx_hashes` Quota Bypassed via Multiple Peer Identities, Enabling Unbounded Map Growth and Transaction Relay Disruption - (File: `sync/src/types/mod.rs`)

---

### Summary

`SyncState::add_ask_for_txs` enforces a per-peer soft limit (`MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` = 32 767) on how many unknown transaction hashes a single peer may contribute to the global `unknown_tx_hashes` priority queue. Because (1) entries are **inserted before** the limit check, (2) the per-peer counter is only evaluated after the global cap is already breached, and (3) **no entries are ever removed when a peer disconnects or is banned**, an attacker who opens multiple peer connections with distinct peer identities can bypass the per-peer quota entirely. Each new identity contributes up to 32 767 fresh garbage hashes before being banned, leaving those hashes permanently in the map. The result is unbounded memory growth of `unknown_tx_hashes` and silent suppression of legitimate transaction announcements from honest peers.

---

### Finding Description

`add_ask_for_txs` in `sync/src/types/mod.rs` processes incoming `RelayTransactionHashes` messages:

```
pub fn add_ask_for_txs(&self, peer_index: PeerIndex, tx_hashes: Vec<Byte32>) -> Status {
    let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();

    // ① Insertion happens unconditionally, BEFORE any limit check
    for tx_hash in tx_hashes.into_iter().take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER) {
        match unknown_tx_hashes.entry(tx_hash) {
            Vacant(entry) => entry.set_priority(UnknownTxHashPriority { ... }),
            Occupied(entry) => { /* add peer to existing entry */ }
        }
    }

    // ② Limit check fires only AFTER insertion
    if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE          // 50 000
        || unknown_tx_hashes.len() >= peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
    {
        // ③ Count this peer's contribution
        let mut peer_unknown_counter = 0;
        for (_hash, priority) in unknown_tx_hashes.iter() {
            for peer in priority.peers.iter() {
                if *peer == peer_index { peer_unknown_counter += 1; }
            }
        }
        if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
            return StatusCode::TooManyUnknownTransactions.into(); // → ban
        }
        return Status::ignored();
    }
    Status::ok()
}
```

Three structural weaknesses combine:

**Weakness 1 – Insert-before-check.** The attacker's hashes are unconditionally inserted at step ①. The ban decision at step ③ comes too late; the entries are already in the map.

**Weakness 2 – Per-peer limit is per-identity, not per-attacker.** The quota is keyed on `PeerIndex`, which is a session-scoped integer assigned at connection time. An attacker who opens a second TCP connection receives a fresh `PeerIndex` and a fresh quota of 32 767 entries.

**Weakness 3 – No cleanup on disconnect/ban.** Searching the codebase reveals no code path that removes entries from `unknown_tx_hashes` when a peer disconnects or is banned. `Peers::disconnected` only updates `n_sync_started` and `n_protected_outbound_peers`. `mark_as_known_txs` removes entries only when a transaction is actually received. Entries contributed by a banned peer therefore persist indefinitely.

The entry point is `TransactionHashesProcess::execute` in `sync/src/relayer/transaction_hashes_process.rs`, which calls `state.add_ask_for_txs(self.peer, tx_hashes)` after a basic batch-size check.

---

### Impact Explanation

1. **Unbounded memory growth.** Each new peer identity inserts up to 32 767 × 32-byte hashes (≈ 1 MiB of keys alone, plus `UnknownTxHashPriority` structs with `Vec<PeerIndex>`) before being banned. With N connections the map grows to N × 32 767 entries. There is no eviction or TTL on `unknown_tx_hashes`.

2. **Transaction relay blackout.** Once `unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE` (50 000), every subsequent `add_ask_for_txs` call from an honest peer that has not yet exceeded its own per-peer counter returns `Status::ignored()` — the announcement is silently dropped and the node never fetches those transactions. This disrupts mempool propagation for the victim node.

3. **Wasted outbound bandwidth and CPU.** `pop_ask_for_txs` periodically drains the queue and sends `GetRelayTransactions` messages to peers for every pending hash. Garbage hashes from the attacker cause the node to send requests that will never be answered, burning bandwidth and lock-contention on the `unknown_tx_hashes` mutex.

---

### Likelihood Explanation

- The attacker is an **unprivileged inbound peer** — no keys, no stake, no special role required.
- Opening multiple TCP connections to a CKB node is trivial; the default `max_peers` is 125, giving an attacker up to 125 simultaneous identities before the peer registry is full.
- The ban duration for `TooManyUnknownTransactions` (StatusCode 416, in the 4xx range) is `BAD_MESSAGE_BAN_TIME` = **5 minutes**. After the ban expires the attacker reconnects with a new session and repeats.
- The attack requires no PoW, no valid transactions, and no coordination — only the ability to open TCP connections and send well-formed `RelayTransactionHashes` messages.

---

### Recommendation

1. **Check before insert.** Evaluate the per-peer counter *before* inserting new hashes. If the peer has already reached `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, return `TooManyUnknownTransactions` immediately without touching the map.

2. **Clean up on peer disconnect/ban.** In the peer-disconnect handler, iterate `unknown_tx_hashes` and remove entries whose `peers` list becomes empty after removing the disconnected `PeerIndex`. Alternatively, store a reverse index `PeerIndex → Vec<Byte32>` to make cleanup O(k) rather than O(n).

3. **Enforce a hard per-peer cap across multiple messages.** Maintain a `HashMap<PeerIndex, usize>` counter that accumulates across all calls from the same peer, not just within a single message.

4. **Add a TTL to `unknown_tx_hashes` entries.** Entries that have been in the queue longer than a configurable timeout (e.g., 2 × `RETRY_ASK_TX_TIMEOUT_INCREASE`) should be expired and removed, bounding the map size independently of peer behavior.

---

### Proof of Concept

1. Attacker opens connection **Peer A** to the victim CKB node.
2. Peer A sends one `RelayTransactionHashes` message containing 32 767 distinct, non-existent tx hashes (all pass the `tx_filter` check because they are unknown).
3. `add_ask_for_txs` inserts all 32 767 entries. Global map size = 32 767 < 50 000 → returns `Status::ok()`. Peer A is **not** banned.
4. Attacker opens connection **Peer B** (new TCP session, new `PeerIndex`).
5. Peer B sends 32 767 more distinct garbage hashes.
6. `add_ask_for_txs` inserts them. Global map size = 65 534 ≥ 50 000 → enters the limit branch. Peer B's counter = 32 767 ≥ `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` → returns `TooManyUnknownTransactions` → Peer B is banned for 5 minutes.
7. **Both Peer A's and Peer B's 65 534 entries remain in the map.** No cleanup occurs.
8. Any honest Peer C that now announces legitimate tx hashes hits `unknown_tx_hashes.len() >= 50 000`, has a counter < 32 767, and receives `Status::ignored()` — its transactions are never fetched by the victim node.
9. After 5 minutes, the attacker reconnects as Peer B′ and repeats, growing the map further.

**Relevant constants** (`util/constant/src/sync.rs`): [1](#0-0) 

**Insertion-before-check root cause** (`sync/src/types/mod.rs`): [2](#0-1) 

**Entry point** (`sync/src/relayer/transaction_hashes_process.rs`): [3](#0-2) 

**Ban decision logic** (`sync/src/status.rs` — 4xx codes trigger `BAD_MESSAGE_BAN_TIME`): [4](#0-3) 

**No cleanup on disconnect** (`sync/src/types/mod.rs` — `Peers::disconnected` does not touch `unknown_tx_hashes`): [5](#0-4)

### Citations

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```

**File:** sync/src/types/mod.rs (L901-923)
```rust
    pub fn disconnected(&self, peer: PeerIndex) {
        if let Some(peer_state) = self.state.remove(&peer).map(|(_, peer_state)| peer_state) {
            if peer_state.sync_started() {
                // It shouldn't happen
                // fetch_sub wraps around on overflow, we still check manually
                // panic here to prevent some bug be hidden silently.
                assert_ne!(
                    self.n_sync_started.fetch_sub(1, Ordering::AcqRel),
                    0,
                    "n_sync_started overflow when disconnects"
                );
            }

            // Protection node disconnected
            if peer_state.peer_flags.is_protect {
                assert_ne!(
                    self.n_protected_outbound_peers
                        .fetch_sub(1, Ordering::AcqRel),
                    0,
                    "n_protected_outbound_peers overflow when disconnects"
                );
            }
        }
```

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

**File:** sync/src/status.rs (L165-179)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        if !(400..500).contains(&(self.code as u16)) {
            return None;
        }
        if let Some(context) = &self.context {
            // TODO: it might be worthwhile to formalize all error texts
            // that won't be banned.
            if context.contains(ARGV_TOO_LONG_TEXT) {
                return None;
            }
        }
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
```
