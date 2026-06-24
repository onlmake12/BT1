Audit Report

## Title
`n_sync_started` Counter Permanently Inflated via TOCTOU Race in `start_sync_headers` — (`sync/src/synchronizer/mod.rs`, `sync/src/types/mod.rs`)

## Summary

In `start_sync_headers`, `n_sync_started` is atomically incremented before `peer_state.start_sync()` is called. If a peer disconnects between these two operations, the `disconnected()` handler observes `sync_started() == false` and skips the decrement, leaving the counter permanently inflated. In IBD mode, a counter value of 1 with no actual syncing peer causes every subsequent call to `start_sync_headers` to break immediately, permanently preventing the node from advancing its chain tip until restarted.

## Finding Description

**Step 1 — atomic increment of `n_sync_started`:**

In `sync/src/synchronizer/mod.rs` lines 672–682, `fetch_update` increments the counter unconditionally (subject to the IBD guard):

```rust
if self.shared().state().n_sync_started()
    .fetch_update(Ordering::AcqRel, Ordering::Acquire, |x| {
        if ibd && x != 0 { None } else { Some(x + 1) }
    })
    .is_err()
{
    break;
}
``` [1](#0-0) 

**Step 2 — conditional `start_sync()` call:**

Immediately after, `get_mut` is called on the `DashMap`. If the peer was already removed by a concurrent `disconnected()` call, `get_mut` returns `None` and `start_sync()` is never called, leaving `headers_sync_state` as `Initialized` (not `Started`):

```rust
if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
    peer_state.start_sync(HeadersSyncController::from_header(&tip));
}
``` [2](#0-1) 

**`disconnected()` only decrements when `sync_started()` is true:**

The handler removes the peer from `state` and checks `sync_started()`. Because `start_sync()` was never called, `headers_sync_state` is still `Initialized`, `started()` returns `false`, and the decrement is skipped: [3](#0-2) 

**`sync_started()` checks for `HeadersSyncState::Started` exactly:** [4](#0-3) [5](#0-4) 

**`n_sync_started` is a plain `AtomicUsize` with no rollback path:** [6](#0-5) 

The `DashMap` provides per-shard locking, not a global lock covering both the `fetch_update` and the `get_mut`. The two operations are not atomic with respect to concurrent peer removal. There is no rollback of the increment anywhere in `start_sync_headers` when `get_mut` returns `None`.

## Impact Explanation

In IBD mode, `if ibd && x != 0 { None }` causes `fetch_update` to return `Err` whenever `n_sync_started >= 1`. Once the counter is inflated to 1 with zero actual syncing peers, every subsequent iteration of the peer loop in `start_sync_headers` immediately breaks. The node cannot initiate header sync with any peer, cannot advance its chain tip, and remains permanently stuck in IBD until restarted. This matches **High: Vulnerabilities which could easily crash a CKB node** — a node permanently unable to sync is functionally equivalent to a crashed node from a network-participation standpoint.

## Likelihood Explanation

The race window is between two consecutive operations in the same function: the `fetch_update` and the `get_mut`. While the window is narrow (microseconds), peer disconnection events are processed by the network layer on separate threads concurrently with the sync loop. A remote peer that connects, passes the `can_start_sync` filter, and then immediately drops the TCP connection can trigger this. No special privileges are required — any inbound or outbound peer can attempt this. With repeated connection/disconnection attempts, the probability of hitting the window accumulates. Once triggered, the impact is persistent (until node restart), making even a low per-attempt probability operationally significant.

## Recommendation

Restructure `start_sync_headers` so the increment and the `start_sync()` call are atomic with respect to peer removal. The safest approach: acquire the peer state entry first, verify the peer exists, call `start_sync()`, and only then increment `n_sync_started`. If `get_mut` returns `None`, skip the increment entirely:

```rust
// Verify peer still exists before incrementing
if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
    // Only increment after confirming peer is present
    if self.shared().state().n_sync_started()
        .fetch_update(Ordering::AcqRel, Ordering::Acquire, |x| {
            if ibd && x != 0 { None } else { Some(x + 1) }
        })
        .is_err()
    {
        break;
    }
    peer_state.start_sync(HeadersSyncController::from_header(&tip));
} else {
    continue; // peer gone, no increment needed
}
```

Alternatively, add a rollback path: if `get_mut` returns `None` after the increment, immediately call `fetch_sub(1, Ordering::AcqRel)` to restore the counter.

## Proof of Concept

1. Node A is in IBD mode (`is_initial_block_download() == true`).
2. Remote peer P connects and completes the sync handshake; P appears in `peers().state` with `can_start_sync() == true`.
3. `start_sync_headers` runs its peer loop for P:
   - `n_sync_started.fetch_update(...)` succeeds → `n_sync_started = 1`.
   - Concurrently, P drops the TCP connection; `disconnected(P)` runs on the network thread, removes P from `state`, observes `sync_started() == false`, does **not** decrement `n_sync_started`.
   - `peers().state.get_mut(&P)` returns `None`; `start_sync()` is never called.
4. `n_sync_started == 1` with zero actual syncing peers.
5. Every subsequent call to `start_sync_headers` hits `if ibd && x != 0 { None }` → `fetch_update` returns `Err` → `break` → no peer ever starts syncing.
6. Node A is permanently stuck in IBD until restarted.

A targeted test can reproduce this by spawning a thread that calls `peers.disconnected(peer)` between the `fetch_update` and `get_mut` calls (using a condvar or sleep to widen the window), then asserting that `n_sync_started.load() == 0` after the race — the assertion will fail, confirming the inflation.

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

**File:** sync/src/types/mod.rs (L313-315)
```rust
    pub(crate) fn sync_started(&self) -> bool {
        self.chain_sync.started()
    }
```

**File:** sync/src/types/mod.rs (L380-385)
```rust
#[derive(Default)]
pub struct Peers {
    pub state: DashMap<PeerIndex, PeerState>,
    pub n_sync_started: AtomicUsize,
    pub n_protected_outbound_peers: AtomicUsize,
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
