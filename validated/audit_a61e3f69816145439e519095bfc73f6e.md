### Title
Global `unknown_tx_hashes` Queue Exhaustion Allows Attacker to DoS Transaction Relay on Any Node - (`sync/src/types/mod.rs`)

---

### Summary

An unprivileged P2P peer can permanently fill the node's global `unknown_tx_hashes` queue by sending `RelayTransactionHashes` messages containing fake transaction hashes. Because hashes are inserted **before** the per-peer limit is enforced, and because entries have no TTL or expiry, the attacker's fake hashes persist in the queue even after the attacker is banned. Once the global queue reaches `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000), all subsequent `RelayTransactionHashes` messages from **legitimate** peers are silently dropped (`Status::ignored()`), causing the victim node to stop fetching new transactions from the network — a complete transaction relay DoS.

---

### Finding Description

`SyncState::add_ask_for_txs` in `sync/src/types/mod.rs` manages the global `unknown_tx_hashes: KeyedPriorityQueue<Byte32, UnknownTxHashPriority>`. This queue is populated whenever a peer sends a `RelayTransactionHashes` P2P message, processed by `TransactionHashesProcess::execute` in `sync/src/relayer/transaction_hashes_process.rs`.

The critical flaw is the **insert-then-check** ordering:

```rust
// 1. Insert up to 32,767 hashes from this peer — BEFORE any limit check
for tx_hash in tx_hashes.into_iter().take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER) {
    match unknown_tx_hashes.entry(tx_hash) {
        Occupied(entry) => { priority.push_peer(peer_index); ... }
        Vacant(entry)   => { entry.set_priority(...) }
    }
}

// 2. AFTER insertion, check if the global queue is full
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE          // 50,000
    || unknown_tx_hashes.len() >= peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
{
    // count entries for this peer
    if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
        return StatusCode::TooManyUnknownTransactions.into(); // peer gets banned
    }
    return Status::ignored(); // legitimate peers silently dropped
}
``` [1](#0-0) 

The attacker's hashes are **already in the queue** when the ban fires. Since `unknown_tx_hashes` has no TTL and entries are only removed by `mark_as_known_txs` (which requires the actual transaction to be received and verified), fake hashes from a banned attacker remain in the queue indefinitely. [2](#0-1) 

The per-peer cap is `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = MAX_RELAY_TXS_NUM_PER_BATCH = 32,767` and the global cap is `MAX_UNKNOWN_TX_HASHES_SIZE = 50,000`. [3](#0-2) 

With only **two attacker connections** (32,767 + 17,233 unique fake hashes), the global queue reaches 50,000 entries. After that, every `RelayTransactionHashes` message from a legitimate peer whose per-peer count is below 32,767 returns `Status::ignored()` — no ban, no error, silent drop. The victim node stops fetching any new transactions from the network. [4](#0-3) 

---

### Impact Explanation

- **Transaction relay is completely DoS'd** on the victim node. The node stops learning about new unconfirmed transactions from all peers.
- Miners running the victim node will miss transactions, reducing fee revenue and causing mempool divergence.
- Users whose transactions are only propagated via the victim node's peers will see their transactions never confirmed.
- The attacker can sustain the attack indefinitely by reconnecting with new identities after each ban, re-filling the queue before entries drain.

---

### Likelihood Explanation

- **Entry path**: Any unprivileged P2P peer can send `RelayTransactionHashes` messages. No authentication, no stake, no PoW required.
- **Cost**: Sending two batches of ~32,767 32-byte hashes each (~2 MB total) over P2P. Trivially cheap.
- **Persistence**: Fake hashes never expire from `unknown_tx_hashes`. The attacker only needs to reconnect after each ban (~5-minute ban time per `BAD_MESSAGE_BAN_TIME`) to top up the queue.
- **Detectability**: The victim node only logs a `warn!` and returns `Status::ignored()` — no alarm, no disconnect of legitimate peers. [5](#0-4) 

---

### Recommendation

1. **Check limits before inserting**: Move the per-peer and global limit checks to the top of `add_ask_for_txs`, before any insertion into `unknown_tx_hashes`. Reject or truncate the input if the limits would be exceeded.

2. **Add TTL/expiry to `unknown_tx_hashes` entries**: Entries for hashes that are never fulfilled (e.g., because the announcing peer was banned) should expire after a bounded time (similar to how `tx_filter` uses `TtlFilter`). This prevents permanent queue exhaustion from banned peers.

3. **Evict entries from banned peers**: When a peer is banned, remove all `unknown_tx_hashes` entries that are exclusively associated with that peer (i.e., `priority.peers == [banned_peer]`).

---

### Proof of Concept

1. Attacker connects to victim node as peer A.
2. Attacker sends one `RelayTransactionHashes` message containing 32,767 unique, non-existent tx hashes.
   - `add_ask_for_txs` inserts all 32,767 hashes into `unknown_tx_hashes`.
   - The per-peer check fires: `peer_unknown_counter = 32,767 >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` → peer A is banned.
   - The 32,767 fake hashes remain in the queue.
3. Attacker reconnects as peer B, sends 17,233 more unique fake hashes.
   - `unknown_tx_hashes.len()` reaches 50,000 = `MAX_UNKNOWN_TX_HASHES_SIZE`.
   - Peer B's per-peer count is 17,233 < 32,767 → `Status::ignored()` (no ban yet, but hashes are inserted).
4. Global queue is now at capacity (50,000 entries, all fake).
5. A legitimate peer C sends a `RelayTransactionHashes` message with real tx hashes.
   - `add_ask_for_txs` inserts them, then checks: `unknown_tx_hashes.len() >= 50,000` → true.
   - Peer C's per-peer count is small → `return Status::ignored()`.
   - The victim node **never requests those transactions** from peer C.
6. Attacker repeats step 2 every ~5 minutes (after ban expires) to maintain the full queue. [6](#0-5) [7](#0-6)

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

**File:** util/constant/src/sync.rs (L59-65)
```rust
/// Default ban time for message
// ban time
// 5 minutes
pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);
/// Default ban time for sync useless
// 10 minutes, peer have no common ancestor block
pub const SYNC_USELESS_BAN_TIME: Duration = Duration::from_secs(10 * 60);
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
