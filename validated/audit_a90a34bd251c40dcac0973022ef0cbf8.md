### Title
Stale Peer Entries in `pending_compact_blocks` Inner Map Not Cleared on Peer Disconnect — (`sync/src/types/mod.rs`)

---

### Summary

When a peer disconnects, `SyncState::disconnected()` cleans up `inflight_blocks` and `peers` state for that peer, but does **not** remove the peer's entry from the inner `HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>` nested inside `pending_compact_blocks`. This stale inner-map state persists until the outer compact-block entry is eventually evicted, and can cause a new peer that is assigned the same `PeerIndex` to be incorrectly rejected or to have its `BlockTransactions` response validated against the wrong (stale) expected-index lists.

---

### Finding Description

`PendingCompactBlockMap` is defined as a two-level map:

```
HashMap<
    Byte32,                                          // block hash (outer key)
    (
        packed::CompactBlock,
        HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>,   // inner: peer → missing tx/uncle indexes
        u64,                                          // timestamp
    ),
>
``` [1](#0-0) 

When a peer disconnects, `SyncState::disconnected()` is called:

```rust
pub fn disconnected(&self, pi: PeerIndex) {
    let removed_inflight_blocks_count = self.write_inflight_blocks().remove_by_peer(pi);
    ...
    self.peers().disconnected(pi);
}
``` [2](#0-1) 

This removes `pi` from `inflight_blocks` and from `peers`, but **never touches `pending_compact_blocks`**. The disconnected peer's entry in the inner `HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>` for every pending compact block remains indefinitely until the outer entry is evicted (block accepted or epoch-based `retain` sweep).

The stale inner entry is then consulted in two critical places:

**1. Duplicate-detection gate in `CompactBlockProcess`:**

```rust
if pending_compact_blocks
    .get(&block_hash)
    .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
    .unwrap_or(false)
{
    return StatusCode::CompactBlockIsAlreadyPending.with_context(block_hash);
}
``` [3](#0-2) 

**2. Expected-index lookup in `BlockTransactionsProcess`:**

```rust
if let Entry::Occupied(mut value) = peers_map.entry(self.peer) {
    let (expected_transaction_indexes, expected_uncle_indexes) = value.get_mut();
    // verification and reconstruction use these stale indexes
``` [4](#0-3) 

The inner map is populated by `missing_or_collided_post_process`:

```rust
.entry(block_hash.clone())
.or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
.1
.insert(peer, (missing_transactions.clone(), missing_uncles.clone()));
``` [5](#0-4) 

---

### Impact Explanation

**Scenario A — Incorrect duplicate rejection (block propagation delay):**
`PeerIndex` values in CKB are session IDs assigned sequentially by the tentacle p2p library. If a peer disconnects and a new peer is assigned the same `PeerIndex` (possible after counter wrap or in high-churn conditions), and that new peer sends a compact block for a hash still pending in the map, the `peers_map.contains_key(&peer)` check returns `true` due to the stale entry. The new peer's compact block is rejected with `CompactBlockIsAlreadyPending`, preventing it from contributing missing transactions and delaying block propagation.

**Scenario B — Wrong expected-index validation:**
If a new peer with a reused `PeerIndex` sends a `BlockTransactions` response for a pending compact block, `BlockTransactionsProcess` retrieves the stale `(expected_transaction_indexes, expected_uncle_indexes)` from the old peer's entry and uses them to verify the new peer's response. The verification will fail or produce an incorrect reconstruction, wasting the response and requiring another round-trip.

**Scenario C — Unbounded inner-map growth:**
An attacker can repeatedly: connect → send a compact block that triggers `Missing` reconstruction → disconnect. Each cycle leaves a stale entry in the inner `peers_map` for every pending compact block. The inner map grows without bound until the outer entry is evicted, constituting a memory amplification vector reachable by any unprivileged peer.

---

### Likelihood Explanation

Any unprivileged peer that can send a compact block requiring missing transactions (a normal network event during IBD or block propagation) and then disconnect triggers the stale-entry condition. Scenario C (memory growth) requires no special conditions. Scenarios A and B require `PeerIndex` reuse, which is less common but possible in long-running nodes or high-churn environments. The attacker-controlled entry path is the standard compact-block relay protocol, reachable by any connected peer.

---

### Recommendation

In `SyncState::disconnected()`, after removing the peer from `inflight_blocks` and `peers`, also iterate over `pending_compact_blocks` and remove the disconnected peer's entry from each inner `peers_map`:

```rust
pub fn disconnected(&self, pi: PeerIndex) {
    let removed_inflight_blocks_count = self.write_inflight_blocks().remove_by_peer(pi);
    ...
    // NEW: clean up stale peer entries in pending_compact_blocks
    let mut pending = self.pending_compact_blocks.blocking_lock();
    for (_, (_, peers_map, _)) in pending.iter_mut() {
        peers_map.remove(&pi);
    }
    drop(pending);

    self.peers().disconnected(pi);
}
```

This mirrors the fix pattern used in `InflightBlocks::remove_by_peer`, which correctly iterates the inner `hashes` set and removes all associated state when a peer disconnects. [6](#0-5) 

---

### Proof of Concept

1. Node A connects to the victim CKB node (peer index = N).
2. Node A sends a compact block for block hash H that is missing transactions. The victim calls `missing_or_collided_post_process`, inserting `(H → (compact_block, {N: (missing_txs, missing_uncles)}, ts))` into `pending_compact_blocks`.
3. Node A disconnects. `SyncState::disconnected(N)` is called. `inflight_blocks` and `peers` are cleaned up, but `pending_compact_blocks[H].peers_map` still contains the entry for peer index N.
4. A new legitimate node B connects and is assigned peer index N (reuse).
5. Node B sends a compact block for the same hash H.
6. `CompactBlockProcess::execute()` checks `peers_map.contains_key(&N)` → `true` (stale entry) → returns `CompactBlockIsAlreadyPending`.
7. Node B cannot contribute to reconstructing block H. The block is delayed until the pending entry times out or is evicted by epoch advancement. [7](#0-6) [8](#0-7)

### Citations

**File:** sync/src/types/mod.rs (L766-783)
```rust
    pub fn remove_by_peer(&mut self, peer: PeerIndex) -> usize {
        let trace = &mut self.trace_number;
        let state = &mut self.inflight_states;

        self.download_schedulers
            .remove(&peer)
            .map(|blocks| {
                let blocks_count = blocks.hashes.iter().len();
                for block in blocks.hashes {
                    state.remove(&block);
                    if !trace.is_empty() {
                        trace.remove(&block);
                    }
                }
                blocks_count
            })
            .unwrap_or_default()
    }
```

**File:** sync/src/types/mod.rs (L979-987)
```rust
// <CompactBlockHash, (CompactBlock, <PeerIndex, (Vec<TransactionsIndex>, Vec<UnclesIndex>)>, timestamp)>
pub(crate) type PendingCompactBlockMap = HashMap<
    Byte32,
    (
        packed::CompactBlock,
        HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>,
        u64,
    ),
>;
```

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

**File:** sync/src/types/mod.rs (L1607-1616)
```rust
    pub fn disconnected(&self, pi: PeerIndex) {
        let removed_inflight_blocks_count = self.write_inflight_blocks().remove_by_peer(pi);
        if removed_inflight_blocks_count > 0 {
            debug!(
                "disconnected {}, remove {} inflight blocks",
                pi, removed_inflight_blocks_count
            )
        }
        self.peers().disconnected(pi);
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L283-291)
```rust
    // compact block is in pending
    let pending_compact_blocks = shared.state().pending_compact_blocks().await;
    if pending_compact_blocks
        .get(&block_hash)
        .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
        .unwrap_or(false)
    {
        return StatusCode::CompactBlockIsAlreadyPending.with_context(block_hash);
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L354-361)
```rust
    shared
        .state()
        .pending_compact_blocks()
        .await
        .entry(block_hash.clone())
        .or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
        .1
        .insert(peer, (missing_transactions.clone(), missing_uncles.clone()));
```

**File:** sync/src/relayer/block_transactions_process.rs (L72-73)
```rust
            if let Entry::Occupied(mut value) = peers_map.entry(self.peer) {
                let (expected_transaction_indexes, expected_uncle_indexes) = value.get_mut();
```
