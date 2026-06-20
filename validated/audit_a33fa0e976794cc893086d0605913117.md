### Title
Single Malicious Peer Can Fill Global `unknown_tx_hashes` Map Before Per-Peer Limit Is Enforced, Silently Dropping Legitimate Peers' Transaction Announcements — (File: `sync/src/types/mod.rs`)

---

### Summary

In `SyncState::add_ask_for_txs`, all incoming tx hashes from a peer are unconditionally inserted into the global `unknown_tx_hashes` map **before** the global size limit is checked. A single unprivileged P2P peer can fill this shared map (capacity 50,000) with phantom tx hashes using only two `RelayTransactionHashes` messages, causing all subsequent announcements from legitimate peers to be silently dropped (`Status::ignored()`). This disables transaction relay for the victim node.

---

### Finding Description

`SyncState::add_ask_for_txs` in `sync/src/types/mod.rs` is called by `TransactionHashesProcess::execute` whenever a peer sends a `RelayTransactionHashes` P2P message.

The function's logic is:

**Step 1 — Insert first, check later (lines 1486–1504):**
All hashes (up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` = 32,767 per call) are inserted into the global `unknown_tx_hashes` map unconditionally. [1](#0-0) 

**Step 2 — Global limit check happens post-insertion (lines 1506–1529):**
Only after insertion does the code check whether the global map has exceeded `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000). The comment at line 1506 explicitly acknowledges this ordering: *"Check `unknown_tx_hashes`'s length **after** inserting the arrival `tx_hashes`"*. [2](#0-1) 

**Step 3 — Per-peer enforcement is also post-insertion:**
If the global limit is exceeded, the code counts how many entries in the entire map belong to the current peer. Only if that count reaches `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32,767) does it return `TooManyUnknownTransactions` (which triggers a ban). Otherwise it returns `Status::ignored()` — silently dropping the message for any other peer. [3](#0-2) 

The constants governing these limits are: [4](#0-3) 

The entry point from the relay protocol: [5](#0-4) 

The `unknown_tx_hashes` map is a node-wide shared structure inside `SyncState`: [6](#0-5) 

Entries are only removed from the map when `mark_as_known_txs` is called (i.e., when the actual transaction is fetched and verified). If the malicious peer never delivers the transactions it announced, the phantom entries persist indefinitely. [7](#0-6) 

---

### Impact Explanation

A single malicious peer can fill the global `unknown_tx_hashes` map (50,000 entries) with phantom tx hashes. Once full, every `RelayTransactionHashes` message from every other peer returns `Status::ignored()`. The victim node stops requesting or propagating new transactions announced by honest peers, effectively disabling its transaction relay subsystem. Legitimate transactions are never fetched, never submitted to the tx-pool, and never relayed to other peers. This is a targeted, persistent DoS against transaction propagation.

---

### Likelihood Explanation

High. Any peer connected to the CKB P2P network can send `RelayTransactionHashes` messages. The attack requires only two messages (each carrying up to 32,767 hashes, the per-message cap enforced in `TransactionHashesProcess::execute`) to reach the 50,000-entry global limit. The per-peer relay rate limiter in `Relayer::try_process` allows 30 requests/second per `(peer, message_type)` key, so two messages pass trivially. [8](#0-7) [9](#0-8) 

---

### Recommendation

1. **Check the per-peer limit before inserting** into the global map. Count the current peer's existing entries first; if already at `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, reject immediately without touching the global map.
2. **Check the global limit before inserting** new entries from a peer that has not yet hit its per-peer limit, so that a single peer cannot consume the entire global budget.
3. **Evict entries belonging to disconnected/banned peers** from `unknown_tx_hashes` on peer disconnect, so that a banned attacker's phantom entries do not persist and block legitimate peers.

---

### Proof of Concept

1. Connect a malicious peer to a CKB node via the Relay protocol.
2. Send `RelayTransactionHashes` with 32,767 unique, non-existent tx hashes → `unknown_tx_hashes` grows to 32,767 entries.
3. Send a second `RelayTransactionHashes` with 17,233 more unique phantom hashes → `unknown_tx_hashes` reaches 50,000 (≥ `MAX_UNKNOWN_TX_HASHES_SIZE`).
4. The malicious peer is not yet banned (its per-peer counter = 50,000 ≥ 32,767 only on the second call, but the global limit is hit first for other peers).
5. Connect a legitimate peer and send `RelayTransactionHashes` with valid, in-mempool tx hashes.
6. `add_ask_for_txs` evaluates: global map ≥ 50,000 → enters the overflow branch → legitimate peer's counter < 32,767 → returns `Status::ignored()`.
7. The node never issues `GetRelayTransactions` for the legitimate peer's hashes; those transactions are never fetched, verified, or relayed.
8. The phantom entries from the malicious peer remain in the map (the node sends `GetRelayTransactions` to the malicious peer, which never responds), keeping the map full indefinitely.

### Citations

**File:** sync/src/types/mod.rs (L1318-1341)
```rust
pub struct SyncState {
    /* Status irrelevant to peers */
    shared_best_header: RwLock<HeaderIndexView>,
    tx_filter: Mutex<TtlFilter<Byte32>>,

    // The priority is ordering by timestamp (reversed), means do not ask the tx before this timestamp (timeout).
    unknown_tx_hashes: Mutex<KeyedPriorityQueue<Byte32, UnknownTxHashPriority>>,

    /* Status relevant to peers */
    peers: Peers,

    /* Cached items which we had received but not completely process */
    pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
    pending_get_headers: RwLock<LruCache<(PeerIndex, Byte32), Instant>>,
    pending_compact_blocks: tokio::sync::Mutex<PendingCompactBlockMap>,

    /* In-flight items for which we request to peers, but not got the responses yet */
    inflight_proposals: DashMap<packed::ProposalShortId, BlockNumber>,
    inflight_blocks: RwLock<InflightBlocks>,

    /* cached for sending bulk */
    tx_relay_receiver: Receiver<TxVerificationResult>,
    min_chain_work: U256,
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

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
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

**File:** sync/src/relayer/mod.rs (L113-123)
```rust
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```
