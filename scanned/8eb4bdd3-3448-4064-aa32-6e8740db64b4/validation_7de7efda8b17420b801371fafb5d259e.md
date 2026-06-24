Audit Report

## Title
`n_sync_started` Counter Permanently Inflated Due to Missing Rollback When Peer Disappears Mid-Transition — (`sync/src/synchronizer/mod.rs`)

## Summary

In `start_sync_headers`, `n_sync_started` is atomically incremented before `peer_state.start_sync()` is called. If the peer is concurrently removed from `peers.state` between those two operations, `get_mut` returns `None`, `start_sync` is never invoked, and `n_sync_started` is never decremented. Because every decrement site guards on `peer_state.sync_started() == true`, and that flag is only set by `start_sync`, no recovery path exists. During IBD the IBD guard permanently blocks all future calls to `start_sync_headers`, freezing header synchronization until the process is restarted.

## Finding Description

`start_sync_headers` in `sync/src/synchronizer/mod.rs` (L670–691) executes two non-atomic steps for each candidate peer:

**Step 1 — increment the counter:** [1](#0-0) 

**Step 2 — mark the peer as started:** [2](#0-1) 

`SyncState::n_sync_started()` is a thin accessor that returns `&self.peers.n_sync_started`: [3](#0-2) 

`peers.state` is a `DashMap<PeerIndex, PeerState>` inside `SyncState`: [4](#0-3) 

`SyncShared` (and thus `SyncState`) is shared between the Sync and Relay protocol handlers: [5](#0-4) 

The Relay handler's `disconnected` callback calls `SyncState::disconnected`, which calls `Peers::disconnected`, which removes the peer from `peers.state`: [6](#0-5) 

`Peers::disconnected` only decrements `n_sync_started` when `peer_state.sync_started() == true`: [7](#0-6) 

`sync_started()` returns `true` only after `start_sync()` has been called, which sets `chain_sync` to the `Started` state: [8](#0-7) [9](#0-8) 

The same guard exists in `suspend_sync` and `tip_synced`: [10](#0-9) [11](#0-10) 

**Race window:** The Sync handler's `notify` callback (which calls `start_sync_headers`) and the Relay handler's `disconnected` callback share the same `SyncState` and run as separate async tasks in the Tokio runtime, meaning they can execute concurrently on different threads. The `DashMap` provides per-shard locking but no cross-operation atomicity between the `fetch_update` and the subsequent `get_mut`. A peer removal that lands between these two lines leaves `n_sync_started` incremented with no corresponding `sync_started() == true` peer, and no code path can ever decrement it back.

**IBD freeze:** The IBD guard `if ibd && x != 0 { None }` causes `fetch_update` to return `Err` whenever `n_sync_started >= 1`. With the counter stuck at 1 and no peer in `Started` state, every subsequent invocation of `start_sync_headers` breaks immediately at the guard without selecting any peer.

## Impact Explanation

A node permanently stuck in IBD cannot download headers, cannot download blocks, and cannot advance its chain tip. It is effectively non-functional as a network participant until the process is restarted. This constitutes a remotely-triggerable, persistent denial-of-service against a CKB node, matching the **High** impact class: *Vulnerabilities which could easily crash a CKB node* (10001–15000 points). The node does not crash but is rendered permanently non-functional for its primary purpose without operator intervention.

## Likelihood Explanation

The race window is narrow — two consecutive lines in the same function — but real. The Sync and Relay handlers run as independent async tasks and can be scheduled concurrently on separate Tokio worker threads. A malicious peer can repeatedly connect and disconnect at high frequency; because `SEND_GET_HEADERS_TOKEN` fires periodically, the attacker receives many attempts per minute. A single successful race permanently corrupts the counter for the lifetime of the process, making the attack asymmetric: low cost to attempt, permanent effect on success.

## Recommendation

**Rollback on miss:** If `get_mut` returns `None`, decrement `n_sync_started` before continuing:

```rust
if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
    peer_state.start_sync(HeadersSyncController::from_header(&tip));
} else {
    self.shared().state().n_sync_started()
        .fetch_sub(1, Ordering::AcqRel);
    continue;
}
```

Alternatively, perform the counter increment and `start_sync` call under the same `DashMap` shard lock (e.g., using `entry` API) so the peer cannot be removed between the two operations. A periodic sanity sweep that recomputes `n_sync_started` from the actual count of peers with `sync_started() == true` would also serve as a safety net.

## Proof of Concept

1. Node enters IBD (`is_initial_block_download() == true`).
2. Attacker peer connects; Sync protocol adds it to `peers.state` with state `SyncProtocolConnected`.
3. `SEND_GET_HEADERS_TOKEN` fires; `start_sync_headers` collects the peer in its candidate list.
4. `fetch_update` succeeds; `n_sync_started` becomes 1.
5. Concurrently, the Relay protocol's `disconnected` callback fires for the same peer (attacker disconnects), calling `SyncState::disconnected` → `Peers::disconnected` → `peers.state.remove(&peer)`. Because `sync_started()` is still `false`, `n_sync_started` is **not** decremented.
6. Back in `start_sync_headers`, `peers.state.get_mut(&peer)` returns `None`; `start_sync` is skipped.
7. `n_sync_started` is now 1 with no peer in `Started` state.
8. All future `start_sync_headers` calls hit `if ibd && x != 0 { None }` → `break` immediately.
9. The node never downloads another header batch; IBD is permanently stalled until restart.

A targeted unit test can reproduce this deterministically by inserting a peer into `peers.state`, calling `n_sync_started().fetch_add(1, ...)` to simulate Step 4, then calling `Peers::disconnected` (which will not decrement because `sync_started()` is false), and asserting that `n_sync_started` remains 1 while no peer has `sync_started() == true` — exactly the stuck state.

### Citations

**File:** sync/src/synchronizer/mod.rs (L672-682)
```rust
            if self
                .shared()
                .state()
                .n_sync_started()
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |x| {
                    if ibd && x != 0 { None } else { Some(x + 1) }
                })
                .is_err()
            {
                break;
            }
```

**File:** sync/src/synchronizer/mod.rs (L683-687)
```rust
            {
                if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
                    peer_state.start_sync(HeadersSyncController::from_header(&tip));
                }
            }
```

**File:** sync/src/types/mod.rs (L297-300)
```rust
    pub fn start_sync(&mut self, headers_sync_controller: HeadersSyncController) {
        self.chain_sync.start();
        self.headers_sync_controller = Some(headers_sync_controller);
    }
```

**File:** sync/src/types/mod.rs (L313-315)
```rust
    pub(crate) fn sync_started(&self) -> bool {
        self.chain_sync.started()
    }
```

**File:** sync/src/types/mod.rs (L901-912)
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
```

**File:** sync/src/types/mod.rs (L990-994)
```rust
#[derive(Clone)]
pub struct SyncShared {
    shared: Shared,
    state: Arc<SyncState>,
}
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

**File:** sync/src/types/mod.rs (L1354-1356)
```rust
    pub fn n_sync_started(&self) -> &AtomicUsize {
        &self.peers.n_sync_started
    }
```

**File:** sync/src/types/mod.rs (L1410-1419)
```rust
    pub(crate) fn suspend_sync(&self, peer_state: &mut PeerState) {
        if peer_state.sync_started() {
            assert_ne!(
                self.peers.n_sync_started.fetch_sub(1, Ordering::AcqRel),
                0,
                "n_sync_started overflow when suspend_sync"
            );
        }
        peer_state.suspend_sync(SUSPEND_SYNC_TIME);
    }
```

**File:** sync/src/types/mod.rs (L1421-1430)
```rust
    pub(crate) fn tip_synced(&self, peer_state: &mut PeerState) {
        if peer_state.sync_started() {
            assert_ne!(
                self.peers.n_sync_started.fetch_sub(1, Ordering::AcqRel),
                0,
                "n_sync_started overflow when tip_synced"
            );
        }
        peer_state.tip_synced();
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
