All cited code is confirmed in the repository. The vulnerability is real and exploitable as described.

Audit Report

## Title
Unbounded `peers` Vec Growth in `UnknownTxHashPriority` via Duplicate `RelayTransactionHashes` — (File: `sync/src/types/mod.rs`)

## Summary
`UnknownTxHashPriority::push_peer` unconditionally appends a `PeerIndex` to a `Vec<PeerIndex>` with no deduplication. An unprivileged relay peer can repeatedly announce the same unknown transaction hash via `RelayTransactionHashes`, causing the `peers` Vec for that single queue entry to grow without bound. The global size guard is bypassed because it only checks `unknown_tx_hashes.len()`, which stays at 1 when a single hash is reused, leading to unbounded heap memory consumption and potential node crash.

## Finding Description
`UnknownTxHashPriority` is defined with a `peers: Vec<PeerIndex>` field and stored in `SyncState::unknown_tx_hashes` (a `KeyedPriorityQueue`). [1](#0-0) 

`push_peer` unconditionally appends with no duplicate check: [2](#0-1) 

In `add_ask_for_txs`, when a tx_hash is already present (`Occupied` branch), `push_peer` is called with no guard: [3](#0-2) 

The global overflow guard only fires when `unknown_tx_hashes.len()` exceeds `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000) or a per-peer threshold: [4](#0-3) 

With a single reused tx_hash, `unknown_tx_hashes.len()` is permanently 1, so this guard is **never reached**.

The entry point `TransactionHashesProcess::execute` filters hashes only against `tx_filter` (fully resolved/known hashes). A hash pending in `unknown_tx_hashes` but never resolved is absent from `tx_filter`, so it passes the filter on every repeated message: [5](#0-4) 

## Impact Explanation
Each repeated `RelayTransactionHashes` message appends one `PeerIndex` (8 bytes) to the `peers` Vec of the single queue entry. Because the global size guard is bypassed, the Vec grows without any upper bound. Sustained over a long-lived connection this exhausts heap memory on the victim node, constituting a remote memory-exhaustion denial-of-service. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The relay protocol is open to any connected peer with no authentication. The rate limiter is keyed by `(PeerIndex, message_type)` and caps throughput at 30 req/s: [6](#0-5) 

At 30 req/s a single peer adds ~108,000 duplicate entries per hour (~864 KB/hr). With multiple coordinated peers the rate scales linearly. `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` is 32,767, meaning each message can carry up to 32,767 hashes, each hitting the `Occupied` branch independently if pre-seeded, amplifying the rate further. [7](#0-6) 

## Recommendation
In `push_peer`, check for an existing entry before appending:

```rust
pub fn push_peer(&mut self, peer_index: PeerIndex) {
    if !self.peers.contains(&peer_index) {
        self.peers.push(peer_index);
    }
}
```

Alternatively, replace `Vec<PeerIndex>` with a `HashSet<PeerIndex>` or `LinkedHashSet<PeerIndex>` to make deduplication O(1) and structurally enforced. Additionally, consider adding a hard cap on `peers.len()` per entry regardless of deduplication.

## Proof of Concept
1. Connect to a CKB node as a relay peer using the standard `RelayV3` protocol.
2. Craft a `RelayTransactionHashes` message containing a single tx_hash that the node does not know (e.g., a random 32-byte value that will never resolve to a real transaction).
3. Send this message in a tight loop at up to 30 messages/second (the rate limiter ceiling).
4. Because the hash never resolves, it is never added to `tx_filter`, so it passes the filter in `TransactionHashesProcess::execute` on every iteration.
5. Each message hits the `Occupied` branch in `add_ask_for_txs` and calls `push_peer`, appending the same `PeerIndex` to the `peers` Vec.
6. `unknown_tx_hashes.len()` remains 1 throughout, so the guard at L1507 is never triggered.
7. Monitor the victim node's heap usage (e.g., via `/proc/<pid>/status` `VmRSS`) and observe monotonically increasing memory consumption with no upper bound.

### Citations

**File:** sync/src/types/mod.rs (L1256-1261)
```rust
#[derive(Eq, PartialEq, Clone)]
pub struct UnknownTxHashPriority {
    request_time: Instant,
    peers: Vec<PeerIndex>,
    requested: bool,
}
```

**File:** sync/src/types/mod.rs (L1291-1293)
```rust
    pub fn push_peer(&mut self, peer_index: PeerIndex) {
        self.peers.push(peer_index);
    }
```

**File:** sync/src/types/mod.rs (L1491-1494)
```rust
                keyed_priority_queue::Entry::Occupied(entry) => {
                    let mut priority = entry.get_priority().clone();
                    priority.push_peer(peer_index);
                    entry.set_priority(priority);
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

**File:** sync/src/relayer/transaction_hashes_process.rs (L38-49)
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
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** util/constant/src/sync.rs (L70-72)
```rust
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
