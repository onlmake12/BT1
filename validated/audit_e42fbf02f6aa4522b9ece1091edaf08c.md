### Title
Global `unknown_tx_hashes` Queue Manipulation via Check-After-Insert Ordering Causes Legitimate Peer Banning - (File: `sync/src/types/mod.rs`)

### Summary

The `add_ask_for_txs` function in `SyncState` inserts attacker-controlled transaction hashes into the global `unknown_tx_hashes` queue **before** checking whether the queue has exceeded its size limit. An unprivileged P2P peer can exploit this ordering to bloat the global queue beyond `MAX_UNKNOWN_TX_HASHES_SIZE`, leaving garbage entries that persist after the attacker is banned. When the queue is in this bloated state, legitimate peers sending the maximum allowed batch of transaction hashes are incorrectly banned with `TooManyUnknownTransactions`, disrupting transaction propagation across the node.

### Finding Description

The root cause is in `add_ask_for_txs` in `sync/src/types/mod.rs`. The function unconditionally inserts all incoming hashes first, then checks the global queue length:

```
// Step 1: Insert ALL hashes (up to MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767)
for tx_hash in tx_hashes.into_iter().take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER) {
    match unknown_tx_hashes.entry(tx_hash) {
        Vacant(entry) => entry.set_priority(...),  // inserted unconditionally
        ...
    }
}

// Step 2: ONLY THEN check if the queue is too large
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE   // 50000
    || unknown_tx_hashes.len() >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
{
    // count this peer's entries and possibly ban
}
```

The attacker entry path is `RelayTransactionHashes` → `TransactionHashesProcess::execute()` → `state.add_ask_for_txs(peer, tx_hashes)`.

**Exploit flow:**

1. Attacker connects and sends a `RelayTransactionHashes` message with 32,767 unique, never-seen hashes (for non-existent transactions). All 32,767 are inserted into `unknown_tx_hashes`. Queue size = 32,767. The check: `32767 < 50000` → no trigger → `Status::ok()`. Attacker is not banned.

2. Attacker sends a second `RelayTransactionHashes` message with another 32,767 unique hashes. All inserted. Queue = 65,534. Check: `65534 >= 50000` → triggers. Per-peer count = 65,534 ≥ 32,767 → `TooManyUnknownTransactions` → attacker is banned.

3. The attacker is now banned, but **65,534 garbage entries remain in the global queue**. These entries reference the banned peer as the only requesting peer. When `pop_ask_for_txs` runs, it calls `next_request_peer()` which returns `None` for a single already-requested peer, so entries are dropped — but only after the next timer tick (~10 seconds, `ASK_FOR_TXS_TOKEN`).

4. During the bloat window, a legitimate peer connects and sends 32,767 valid tx hashes. All 32,767 are inserted (queue = 65,534 + 32,767 = 98,301). Check: `98301 >= 50000` → triggers. Per-peer count for the legitimate peer = 32,767 ≥ `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32,767) → `TooManyUnknownTransactions` → **legitimate peer is banned**.

5. The attacker reconnects after the ban expires and repeats, keeping the queue perpetually bloated and continuously causing legitimate peers to be banned.

The `tx_filter` does not prevent this: it only tracks hashes of transactions that have been seen/verified. The attacker uses fresh, never-seen hashes each reconnection.

### Impact Explanation

- **Legitimate peer banning**: Any peer sending the maximum allowed batch of 32,767 tx hashes while the queue is bloated will be banned with `TooManyUnknownTransactions`. This is a false positive ban that disrupts transaction relay.
- **Transaction propagation disruption**: Banned legitimate peers cannot relay transactions to the victim node, degrading mempool synchronization and potentially delaying transaction confirmation.
- **Persistent state**: The bloated queue persists for at least one timer cycle (~10 seconds) after the attacker is banned, and the attacker can reconnect to repeat the attack indefinitely.
- The global `unknown_tx_hashes` is a node-wide shared resource in `SyncState`; all peers share it, so one attacker affects all concurrent legitimate peers.

### Likelihood Explanation

- Requires only a standard P2P connection — no privileged access, no keys, no hashpower.
- The attack requires sending exactly 2 `RelayTransactionHashes` messages with 32,767 unique hashes each. This is within the protocol's allowed message size (`MAX_RELAY_TXS_NUM_PER_BATCH = 32767`).
- The per-peer rate limiter (30 req/s) does not prevent this — only 2 messages are needed.
- The attacker can reconnect after the ban expires (default ban time for this status code applies) and repeat indefinitely.
- No coordination or special resources required beyond a single network connection.

### Recommendation

Move the size-limit check **before** insertion. Reject or truncate the incoming batch if adding it would exceed the per-peer or global limit, rather than inserting first and checking after. Specifically:

1. Before the insertion loop, compute how many new unique hashes this peer would add.
2. If the peer's current contribution already meets or exceeds `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, return `TooManyUnknownTransactions` immediately without inserting anything.
3. If the global queue is already at or above `MAX_UNKNOWN_TX_HASHES_SIZE`, return `Status::ignored()` without inserting anything.

This ensures the global queue never grows beyond the intended limit due to attacker-controlled input.

### Proof of Concept

```
1. Connect to a CKB node as a P2P peer supporting RelayV3.

2. Send RelayTransactionHashes message #1:
   - tx_hashes: [H1, H2, ..., H32767]  (32767 unique, non-existent tx hashes)
   - Result: all inserted into unknown_tx_hashes, Status::ok(), not banned.
   - Global queue size: 32767

3. Send RelayTransactionHashes message #2:
   - tx_hashes: [H32768, H32769, ..., H65534]  (32767 more unique hashes)
   - Result: all inserted, check fires, attacker banned.
   - Global queue size: 65534 (garbage entries remain)

4. Immediately (within ~10 seconds), connect as a legitimate peer and send:
   - RelayTransactionHashes with 32767 valid tx hashes
   - Result: all inserted (queue = 98301), check fires, per-peer count = 32767 >= 32767
   - Legitimate peer receives TooManyUnknownTransactions ban.

5. Repeat from step 1 after ban expires to maintain persistent disruption.
```

**Key code references:** [1](#0-0) 

The insertion loop at lines 1486–1504 runs unconditionally before the size check at lines 1506–1529. [2](#0-1) 

The attacker-controlled entry point: `TransactionHashesProcess::execute()` calls `state.add_ask_for_txs(self.peer, tx_hashes)` with no pre-insertion guard. [3](#0-2) 

`MAX_RELAY_TXS_NUM_PER_BATCH = 32767`, `MAX_UNKNOWN_TX_HASHES_SIZE = 50000`, `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767` — the per-peer limit equals the per-message limit, so a single message can saturate the per-peer quota in one shot. [4](#0-3) 

`SyncState.unknown_tx_hashes` is a single global `Mutex<KeyedPriorityQueue>` shared across all peers, making it the manipulable global accumulator analogous to the `TimeLockStrategy` daily limit.

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

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
