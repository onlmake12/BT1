Audit Report

## Title
Unbounded `peers` Vec Growth via Repeated `RelayTransactionHashes` in `add_ask_for_txs` — (`sync/src/types/mod.rs`)

## Summary
`SyncState::add_ask_for_txs` calls `push_peer(peer_index)` on every `Occupied` entry without deduplication or a per-entry size cap. The post-insertion guard checks only the count of distinct hash keys in `unknown_tx_hashes`, which does not change when the same hashes are re-sent. An attacker who first populates the map with 32,767 hashes can then replay the identical message in a tight loop, growing each entry's `peers` Vec by one element per call with no bound, exhausting node memory.

## Finding Description
`UnknownTxHashPriority` stores interested peers as a plain `Vec<PeerIndex>` with no size limit:

```rust
// sync/src/types/mod.rs L1256-1261
pub struct UnknownTxHashPriority {
    request_time: Instant,
    peers: Vec<PeerIndex>,
    requested: bool,
}
```

`push_peer` appends unconditionally:

```rust
// sync/src/types/mod.rs L1291-1293
pub fn push_peer(&mut self, peer_index: PeerIndex) {
    self.peers.push(peer_index);
}
```

In `add_ask_for_txs`, every hash already present in the keyed-priority-queue hits the `Occupied` branch and calls `push_peer` with no existence check or cap:

```rust
// sync/src/types/mod.rs L1491-1494
keyed_priority_queue::Entry::Occupied(entry) => {
    let mut priority = entry.get_priority().clone();
    priority.push_peer(peer_index);
    entry.set_priority(priority);
}
```

The only guard fires when `unknown_tx_hashes.len()` (the count of *distinct hash keys*) crosses a threshold:

```rust
// sync/src/types/mod.rs L1507-1509
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
    || unknown_tx_hashes.len()
        >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
```

When an attacker replays the same set of hashes, all entries hit the `Occupied` branch — no new keys are inserted — so `unknown_tx_hashes.len()` stays constant and the guard never fires. The per-peer counter check inside the guard (lines 1516–1526) is therefore also never reached. `push_peer` is called unconditionally on every hash in every repeated message, growing each entry's `peers` Vec without bound. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

## Impact Explanation
**High — Vulnerabilities which could easily crash a CKB node.**

Each `PeerIndex` is a `usize` (8 bytes on 64-bit). With 32,767 hashes per message and N repeated sends, the `peers` Vec entries alone consume `32,767 × N × 8` bytes. At N = 10,000 sends the `peers` Vecs alone exceed 2.6 GB, causing an OOM crash. The crash halts block and transaction relay for all connected peers. The impact is concrete and reproducible, not theoretical. [5](#0-4) 

## Likelihood Explanation
The attack requires only a standard P2P connection — no privileged role, no key material, no majority hashpower. The attacker sends one `RelayTransactionHashes` message with 32,767 hashes to populate the map (below `MAX_UNKNOWN_TX_HASHES_SIZE` = 50,000), then replays the identical message in a tight loop. Each message is small and constant-size. The protocol imposes no rate limit on this message type beyond the per-call `take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)` truncation, which does not prevent repeated calls. The attack is cheap for the attacker and unboundedly expensive for the victim. [6](#0-5) 

## Recommendation
1. **Deduplicate `peer_index` in `push_peer`**: replace `Vec<PeerIndex>` with `HashSet<PeerIndex>` for the `peers` field of `UnknownTxHashPriority`, or check for existence before pushing.
2. **Cap the `peers` collection per entry**: enforce a hard maximum on the number of peers recorded per hash entry and silently drop the push when the cap is reached.
3. **Move the per-peer limit to a pre-insertion guard**: count the peer's existing entries *before* the insertion loop, unconditionally on every call, not only when the global distinct-key threshold is crossed.

## Proof of Concept
```
1. Attacker peer connects to a CKB node via the Relay protocol.
2. Attacker constructs a RelayTransactionHashes message containing
   32,767 transaction hashes unknown to the node.
3. Attacker sends the message once — unknown_tx_hashes now has 32,767
   distinct keys (below MAX_UNKNOWN_TX_HASHES_SIZE = 50,000).
4. Attacker sends the identical message in a tight loop (N iterations).
   - Each iteration: all 32,767 hashes hit the Occupied branch.
   - push_peer(attacker_peer) appends to each entry's peers Vec.
   - unknown_tx_hashes.len() stays at 32,767; the guard never fires.
5. After N = 10,000 iterations:
   - Each of the 32,767 entries holds a peers Vec of length 10,001.
   - Memory for peers Vecs alone: 32,767 × 10,001 × 8 ≈ 2.6 GB.
6. Node OOMs and crashes.

Unit test verification: call add_ask_for_txs 1,000 times with the
same peer_index and same 32,767 hashes; then iterate unknown_tx_hashes
and assert that no entry's peers.len() exceeds 1. With the current
code, each entry will have peers.len() == 1,000, confirming the bug.
```

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
