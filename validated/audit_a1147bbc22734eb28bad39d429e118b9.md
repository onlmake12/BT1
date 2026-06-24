Audit Report

## Title
`n_sync_started` Incremented Before Peer State Committed, Enabling Permanent IBD Header-Sync Stall - (File: `sync/src/synchronizer/mod.rs`)

## Summary

In `start_sync_headers`, `n_sync_started` is atomically incremented via `fetch_update` at line 676 before the peer's `HeadersSyncState` is transitioned to `Started` via `peer_state.start_sync()` at line 685. If a peer disconnects between these two operations, `Peers::disconnected` skips the counter decrement because `sync_started()` still returns `false`. The counter is permanently stuck at 1 with no peer in `Started` state, and the IBD single-peer guard prevents any future peer from being selected for header sync, permanently stalling the node in IBD.

## Finding Description

`start_sync_headers` performs two logically coupled but non-atomic operations in sequence:

**Step 1** — `fetch_update` increments `n_sync_started` to 1: [1](#0-0) 

**Step 2** — acquires the `DashMap` write-lock and calls `start_sync()`: [2](#0-1) 

Between steps 1 and 2, the peer's `HeadersSyncState` is still `SyncProtocolConnected`, so `sync_started()` returns `false`: [3](#0-2) 

If the peer disconnects in this window, `Peers::disconnected` removes the peer from `peers.state`, checks `peer_state.sync_started()` (which is `false`), and does **not** decrement `n_sync_started`: [4](#0-3) 

The subsequent `get_mut(&peer)` in step 2 returns `None` for the already-removed peer, so `start_sync()` is never called. The counter is now permanently 1 with no peer in `Started` state.

Every path that decrements `n_sync_started` — `disconnected`, `suspend_sync`, and `tip_synced` — gates on `sync_started() == true`: [5](#0-4) [6](#0-5) 

Since no peer ever reaches `Started` state in this scenario, none of these paths fire, and the counter is permanently stuck at 1. The node cannot exit IBD through any other mechanism because header sync is the prerequisite for advancing the chain tip, making the stall self-reinforcing.

## Impact Explanation

In IBD mode, `start_sync_headers` enforces a single-peer constraint via the `fetch_update` closure at line 677: `if ibd && x != 0 { None } else { Some(x + 1) }`. With `n_sync_started` permanently at 1, every subsequent call during IBD causes `fetch_update` to return `Err` and immediately `break` the loop. No peer is ever selected for header sync again. The node is permanently stalled in IBD and cannot advance its chain tip, rendering it non-functional as a network participant. This matches the **High** severity impact class: "Vulnerabilities which could easily crash a CKB node" (10001–15000 points), as a permanent functional stall is equivalent to a crash from an operational standpoint. [1](#0-0) 

## Likelihood Explanation

The race window spans only a few instructions between the `fetch_update` at line 676 and the `get_mut` at line 684, but both operations touch concurrent data structures (`AtomicUsize` and `DashMap`) explicitly designed for multi-threaded access, confirming the code runs in a concurrent context. An unprivileged peer can connect and immediately close the TCP connection to trigger `disconnected` concurrently with the timer-driven `start_sync_headers`. The timer fires periodically, giving an attacker unlimited repeated attempts with no special privileges, keys, or majority hashpower required. A single successful race hit permanently corrupts the counter.

## Recommendation

Ensure `n_sync_started` is only incremented **after** `peer_state.start_sync()` has been successfully applied. The fix should: (1) acquire the `DashMap` write-lock on the peer entry first, (2) verify the peer still exists, (3) call `start_sync()`, and only then (4) increment `n_sync_started`. Alternatively, hold the peer's `DashMap` guard across both operations so a concurrent `disconnected` call cannot remove the peer between them. This eliminates the window entirely by making the state transition and counter increment a single logical unit.

## Proof of Concept

1. Node enters IBD. `n_sync_started == 0`.
2. `start_sync_headers` iterates eligible peers and selects peer P.
3. `fetch_update` succeeds: `n_sync_started` becomes 1. Peer P's `HeadersSyncState` is still `SyncProtocolConnected`.
4. Attacker (peer P) closes the TCP connection. The network layer fires `disconnected(P)` concurrently.
5. `Peers::disconnected` removes P from `peers.state`; `sync_started()` returns `false`; `n_sync_started` is **not** decremented. `n_sync_started == 1`.
6. `start_sync_headers` attempts `get_mut(&P)` → `None`; `start_sync()` is never called. No peer is in `Started` state.
7. All future `start_sync_headers` calls in IBD mode hit `ibd && x != 0` → `fetch_update` returns `Err` → `break`. No peer is ever synced. Node is permanently stalled in IBD.

To reproduce deterministically: write a unit test that (a) calls `fetch_update` on `n_sync_started` to simulate step 3, (b) calls `peers.disconnected(P)` before `get_mut`, and (c) asserts `n_sync_started.load() == 1` with no peer in `Started` state, then verifies that a subsequent `start_sync_headers` call in IBD mode selects zero peers.

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

**File:** sync/src/types/mod.rs (L107-109)
```rust
    fn started(&self) -> bool {
        matches!(self.headers_sync_state, HeadersSyncState::Started)
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

**File:** sync/src/types/mod.rs (L1410-1418)
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
