Based on my code review, the claim is accurate. Here is the validated finding:

Audit Report

## Title
`n_sync_started` Counter Permanently Inflated via Peer Disconnect Race in `start_sync_headers` — (File: sync/src/synchronizer/mod.rs)

## Summary

In `start_sync_headers`, the global `n_sync_started` counter is incremented atomically before the per-peer `HeadersSyncState` is transitioned to `Started`. If a peer disconnects between these two non-atomic steps, `disconnected()` observes the peer's state as `SyncProtocolConnected`, skips the counter decrement, and removes the peer. The counter is permanently inflated by 1. During IBD, this causes the node to refuse to start header sync with any peer, stalling initial block download indefinitely until restart.

## Finding Description

**Root cause — two-step, non-atomic update:**

In `start_sync_headers` (sync/src/synchronizer/mod.rs, lines 672–687), the counter increment and the state transition are two separate, non-atomic operations:

```rust
// Step 1: counter incremented atomically (lines 672–682)
if self.shared().state().n_sync_started()
    .fetch_update(Ordering::AcqRel, Ordering::Acquire, |x| {
        if ibd && x != 0 { None } else { Some(x + 1) }
    })
    .is_err()
{
    break;
}
// Step 2: state set to Started (lines 683–687) — separate, non-atomic step
if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
    peer_state.start_sync(HeadersSyncController::from_header(&tip));
}
```

**`HeadersSyncState` enum** (sync/src/types/mod.rs, lines 119–127) has five variants: `Initialized`, `SyncProtocolConnected`, `Started`, `Suspend(u64)`, `TipSynced(u64)`. The `started()` predicate (lines 107–109) returns `true` only for `Started`.

**`disconnected()`** (sync/src/types/mod.rs, lines 901–924) removes the peer from `state` and only decrements `n_sync_started` if `peer_state.sync_started()` is `true`:

```rust
pub fn disconnected(&self, peer: PeerIndex) {
    if let Some(peer_state) = self.state.remove(&peer).map(|(_, ps)| ps) {
        if peer_state.sync_started() {   // false if state is SyncProtocolConnected
            self.n_sync_started.fetch_sub(1, Ordering::AcqRel);  // ← SKIPPED
        }
    }
}
```

**Race window:** Between Step 1 (counter incremented, peer still in `SyncProtocolConnected`) and Step 2 (`get_mut` sets state to `Started`), `disconnected()` can fire. It removes the peer with state `SyncProtocolConnected`, `sync_started()` returns `false`, the decrement is skipped. Back in `start_sync_headers`, `get_mut(&peer)` returns `None` (peer already removed), so the state is never set to `Started`. Counter is now `1` with no peer in `Started` state — permanently leaked.

**IBD guard** (line 677) then blocks all future sync:
```rust
if ibd && x != 0 { None }  // x == 1 forever → always Err → always break
```

The use of `DashMap` and `AtomicUsize` confirms concurrent multi-threaded access is architecturally expected. The test comment at line 1090 of `sync/src/tests/synchronizer/functions.rs` explicitly acknowledges: *"There may be competition between header sync and eviction, it will case assert panic"* — confirming developer awareness of concurrent execution between sync and disconnect paths.

## Impact Explanation

During IBD, `n_sync_started != 0` causes `start_sync_headers` to `break` immediately on every timer tick without starting sync with any peer. A permanently inflated counter means the node can never advance past IBD, as it cannot initiate header synchronization with any connected peer. The node is effectively stalled at genesis until restarted. This maps to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node** — a node permanently unable to sync is functionally equivalent to a crashed node from the perspective of network participation.

## Likelihood Explanation

The race window is narrow (between two consecutive non-awaited lines), but the attack is cheap and repeatable: connect as a peer, wait for the 1-second `SYNC_NOTIFY_INTERVAL` timer tick, disconnect at the right moment. A single successful race permanently stalls the node. The attacker can retry at zero cost until the race is won. `DashMap` and `AtomicUsize` confirm the concurrent access model is real, not theoretical.

## Recommendation

The increment of `n_sync_started` and the transition of `HeadersSyncState` to `Started` must be performed as a single atomic operation. The recommended fix is to hold the `DashMap` entry's write guard across both the counter increment and the state update, so that a concurrent `disconnected()` either sees the full `Started` state (and decrements) or sees the peer already removed (and the counter was never incremented). Alternatively, add a compensating decrement in `start_sync_headers` after the `get_mut` call returns `None` following a successful `fetch_update`, to undo the increment when the peer has already been removed.

## Proof of Concept

**Precondition:** Victim node is in IBD (`is_initial_block_download() == true`), `n_sync_started == 0`.

1. Attacker connects to victim as peer P. `sync_connected()` sets P's state to `SyncProtocolConnected`.
2. `SEND_GET_HEADERS_TOKEN` timer fires → `start_sync_headers` runs.
3. P passes `can_start_sync()` (state is `SyncProtocolConnected`).
4. `n_sync_started.fetch_update(...)` succeeds → counter becomes `1`.
5. **Attacker disconnects P** (races between the `fetch_update` return and the `get_mut` call).
6. `Peers::disconnected()`: `state.remove(&P)` returns `PeerState` with `HeadersSyncState::SyncProtocolConnected`; `sync_started()` → `false` → counter NOT decremented. Counter stays at `1`.
7. Back in `start_sync_headers`: `self.peers().state.get_mut(&P)` → `None` → `start_sync` never called.
8. Counter is `1`, no peer is in `Started` state.
9. On every subsequent `SEND_GET_HEADERS_TOKEN` tick: `fetch_update` returns `Err` (`ibd && x != 0`), loop breaks immediately. No sync ever starts.
10. Node is permanently stalled in IBD until restarted.

A unit test can reproduce this by: (a) constructing a `Peers` instance, (b) calling the counter increment path directly, (c) calling `disconnected()` before `start_sync()`, and (d) asserting `n_sync_started == 1` with no peer in `Started` state, then verifying that subsequent `start_sync_headers` invocations with `ibd=true` never start sync.