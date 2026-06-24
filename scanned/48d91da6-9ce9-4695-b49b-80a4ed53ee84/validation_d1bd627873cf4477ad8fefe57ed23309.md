Audit Report

## Title
Insert-Before-Check with O(N) Mutex-Held Linear Scan in `add_ask_for_txs` — (`sync/src/types/mod.rs`)

## Summary

`SyncState::add_ask_for_txs` unconditionally inserts up to 32,767 hashes into the global `unknown_tx_hashes` queue before evaluating the global size limit. Once the queue reaches `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000), every subsequent call performs a full O(N) nested linear scan over all queue entries while holding the `unknown_tx_hashes` mutex. The overflow path returns `Status::ignored()` with no peer penalty, allowing an attacker with two standard P2P connections to permanently keep the queue above the threshold and serialize all callers of the shared mutex.

## Finding Description

**Entry point** — `TransactionHashesProcess::execute` (`sync/src/relayer/transaction_hashes_process.rs`, lines 25–50) applies only a per-message count guard (`> MAX_RELAY_TXS_NUM_PER_BATCH`). There is no per-peer rate limit or cooldown. After filtering through `tx_filter`, it calls `state.add_ask_for_txs(self.peer, tx_hashes)` unconditionally. [1](#0-0) 

**Insert-before-check** — `add_ask_for_txs` acquires the mutex at line 1484 and holds it for the entire function. Lines 1486–1504 insert up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32,767) new entries unconditionally. The global size check only occurs after all insertions complete. [2](#0-1) 

**O(N) scan on overflow** — When `unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE` (50,000), lines 1517–1522 iterate over every entry in the queue and every peer inside each entry's `peers` vector to count the calling peer's contributions. This is a nested O(N×P) scan (N = queue size, P = average peers per entry) executed while the mutex is held. [3](#0-2) 

**No penalty on overflow** — When the per-peer limit is not exceeded, the function returns `Status::ignored()` at line 1528. The attacker is never banned or disconnected, making the overflow path freely and repeatedly triggerable. [4](#0-3) 

**Shared mutex contention** — Both `pop_ask_for_txs` (line 1454) and `mark_as_known_txs` (line 1444) acquire the same `self.unknown_tx_hashes` mutex. While `add_ask_for_txs` holds the lock executing the O(N) scan, both callers block: the `ASK_FOR_TXS_TOKEN` timer cannot dispatch `GetRelayTransactions` requests, and verified transactions cannot be removed from the queue. [5](#0-4) 

**Constants** — `MAX_RELAY_TXS_NUM_PER_BATCH = MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32,767` and `MAX_UNKNOWN_TX_HASHES_SIZE = 50,000`, meaning two peers each sending one full batch are sufficient to exceed the global limit. [6](#0-5) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Each `add_ask_for_txs` call when the queue is saturated performs: (a) up to 32,767 hash-map insertions, then (b) a full O(N) nested scan over ≥50,000 entries, all under the shared mutex. This serializes `pop_ask_for_txs` (the tx-relay dispatch timer) and `mark_as_known_txs` (post-verification cleanup). Stalling the dispatch timer means `GetRelayTransactions` requests are not sent, degrading transaction propagation throughput on the affected node. Applied concurrently across multiple nodes by a low-resource attacker, this constitutes network-wide transaction relay congestion achievable at very low cost.

## Likelihood Explanation

The attack requires only two standard P2P connections (no PoW, stake, or privilege) and approximately 2 × 32,767 × 32 bytes ≈ 2 MB of unique fake hashes to saturate the queue. The `tx_filter` TTL filter does not prevent reuse of fresh hashes. Because the overflow response is silent (`Status::ignored()`), the attacker receives no feedback and is never disconnected. The queue can be kept above the threshold indefinitely with a low-rate trickle of new hashes to offset `pop_ask_for_txs` draining. The attack is fully repeatable and requires no victim interaction.

## Recommendation

1. **Check before insert**: Evaluate both the per-peer and global limits before inserting any hashes; reject the entire batch early if either limit would be exceeded, avoiding unnecessary insertions and the subsequent scan.
2. **Replace the O(N) scan with O(1) accounting**: Maintain a per-peer counter (e.g., in `PeerState`) incremented on insertion and decremented on removal, eliminating the need to scan the queue at all.
3. **Enforce a hard per-peer cap at insertion time**: Once a peer's counter reaches `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, reject further hashes from that peer without touching the global queue or acquiring the global mutex for longer than necessary.
4. **Penalize overflow triggers**: Return a ban or disconnect status (e.g., `StatusCode::TooManyUnknownTransactions`) rather than `Status::ignored()` when a peer repeatedly drives the queue above the global limit.

## Proof of Concept

```
1. Connect two attacker peers A and B to the target node via standard P2P.
2. Peer A sends RelayTransactionHashes with 32,767 unique fake 32-byte hashes.
   → unknown_tx_hashes.len() ≈ 32,767 (below MAX_UNKNOWN_TX_HASHES_SIZE).
3. Peer B sends RelayTransactionHashes with 32,767 different unique fake hashes.
   → unknown_tx_hashes.len() ≈ 65,534 (exceeds MAX_UNKNOWN_TX_HASHES_SIZE = 50,000).
   → Overflow branch fires; O(N) scan over ~65,534 entries executes under mutex.
4. Send any subsequent RelayTransactionHashes from any peer.
   → add_ask_for_txs acquires mutex, inserts up to 32,767 entries, then scans all ~50,000+ entries.
5. Instrument pop_ask_for_txs and mark_as_known_txs call latency; observe blocking
   proportional to queue size vs. baseline (empty queue).
6. Flood with concurrent messages from multiple connections; confirm that
   pop_ask_for_txs timer dispatch and mark_as_known_txs are delayed proportionally,
   degrading tx-relay throughput.
7. Verify no ban or disconnect is issued to attacker peers (Status::ignored() returned).
```

### Citations

**File:** sync/src/relayer/transaction_hashes_process.rs (L29-49)
```rust
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
```

**File:** sync/src/types/mod.rs (L1444-1454)
```rust
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
        let mut tx_filter = self.tx_filter.lock();

        for hash in hashes {
            unknown_tx_hashes.remove(&hash);
            tx_filter.insert(hash);
        }
    }

    pub fn pop_ask_for_txs(&self) -> HashMap<PeerIndex, Vec<Byte32>> {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
```

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

**File:** util/constant/src/sync.rs (L67-72)
```rust
/// The maximum number transaction hashes inside a `RelayTransactionHashes` message
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
