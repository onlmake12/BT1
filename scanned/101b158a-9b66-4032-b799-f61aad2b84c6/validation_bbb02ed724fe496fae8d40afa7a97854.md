Audit Report

## Title
Non-Atomic `n_sync_started` Increment Causes Permanent IBD Stall — (`sync/src/synchronizer/mod.rs`)

## Summary

In `start_sync_headers`, the global atomic counter `n_sync_started` is incremented before the per-peer `headers_sync_state` is set to `Started` via `peer_state.start_sync()`. These two operations are not atomic with respect to concurrent peer disconnection. If a peer disconnects between the increment and the `start_sync` call, `n_sync_started` is permanently inflated by 1. In IBD mode, this permanently prevents any new peer from starting header sync, rendering the node non-functional indefinitely.

## Finding Description

In `start_sync_headers` (`sync/src/synchronizer/mod.rs`, L670–691), the loop first atomically increments `n_sync_started` via `fetch_update`, then separately acquires a `DashMap` write guard to call `peer_state.start_sync()`:

```rust
// Step 1: increment counter
if self.shared().state().n_sync_started()
    .fetch_update(Ordering::AcqRel, Ordering::Acquire, |x| {
        if ibd && x != 0 { None } else { Some(x + 1) }
    }).is_err() { break; }

// Step 2: set per-peer state (separate, non-atomic)
if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
    peer_state.start_sync(HeadersSyncController::from_header(&tip));
}
``` [1](#0-0) 

The decrement of `n_sync_started` in `Peers::disconnected()` is conditional on `peer_state.sync_started()` returning `true`:

```rust
pub fn disconnected(&self, peer: PeerIndex) {
    if let Some(peer_state) = self.state.remove(&peer)... {
        if peer_state.sync_started() {
            self.n_sync_started.fetch_sub(1, Ordering::AcqRel);
        }
    }
}
``` [2](#0-1) 

`sync_started()` returns `true` only when `headers_sync_state == HeadersSyncState::Started`, which is set exclusively by `peer_state.start_sync()`: [3](#0-2) 

**Race window:** Between Step 1 and Step 2, a concurrent network event can call `Peers::disconnected()` for the same peer. At that moment, `sync_started()` is still `false` (Step 2 has not executed), so `n_sync_started` is not decremented. When control returns to `start_sync_headers`, `self.peers().state.get_mut(&peer)` returns `None` (peer already removed), so `start_sync` is never called. The counter is permanently inflated by 1.

The IBD gate in `fetch_update` is:
```rust
if ibd && x != 0 { None } else { Some(x + 1) }
``` [4](#0-3) 

Once `n_sync_started` is stuck at ≥ 1, every subsequent call to `start_sync_headers` in IBD mode immediately `break`s. There is no reset, recomputation, or administrative recovery path for `n_sync_started`. [5](#0-4) 

## Impact Explanation

A permanently inflated `n_sync_started` during IBD causes the node to be unable to select any peer for header sync. The node remains stuck in IBD indefinitely: it cannot advance its chain tip, block relay is degraded, the tx-pool is stalled, and RPC chain state is frozen. This constitutes a permanent functional failure of the node, matching **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**. The node process does not terminate, but it becomes permanently non-functional for its primary purpose.

## Likelihood Explanation

The race window is the interval between two consecutive lines of code executing in an async tokio task. CKB uses a multi-threaded tokio runtime, so the network event handler (calling `disconnected`) and the sync timer callback (calling `start_sync_headers`) can execute concurrently on different threads. An unprivileged attacker needs only the ability to make and drop TCP connections to the node's P2P port. By repeatedly connecting and immediately disconnecting, the attacker maximizes the probability of hitting the race window. No keys, hashpower, or special privileges are required. The attack is repeatable and low-cost.

## Recommendation

Acquire the `DashMap` write guard on the peer state **before** incrementing `n_sync_started`. This ensures that if the peer has already been removed by `disconnected()`, the counter is never incremented:

```rust
for peer in peers {
    if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
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
        peer_state.start_sync(HeadersSyncController::from_header(&tip));
    }
    // If peer is gone, skip without incrementing
}
```

This makes the check-then-increment-then-set sequence atomic with respect to peer removal, because `DashMap::get_mut` holds a shard lock that prevents concurrent removal of the same entry.

## Proof of Concept

**Manual steps:**

1. Start a CKB node from genesis (IBD mode, `n_sync_started == 0`).
2. Connect an attacker-controlled peer; `sync_connected` is called, peer is added to `Peers::state`.
3. The sync timer fires `start_sync_headers`; the peer passes `can_start_sync`.
4. `n_sync_started.fetch_update(...)` succeeds → `n_sync_started = 1`.
5. Attacker peer immediately disconnects. `Peers::disconnected()` fires concurrently on another thread:
   - Peer is removed from `self.state`.
   - `peer_state.sync_started()` → `false` (Step 2 not yet executed).
   - `n_sync_started` is **not** decremented. Remains `1`.
6. Back in `start_sync_headers`: `self.peers().state.get_mut(&peer)` → `None`. `start_sync` is never called.
7. `n_sync_started` is permanently `1`.
8. All future calls to `start_sync_headers` in IBD mode: `fetch_update` returns `Err` immediately, loop breaks. No peer ever starts syncing.
9. Node is permanently stuck in IBD.

**Fuzz/invariant test plan:** Instrument `n_sync_started` with an invariant checker that asserts `n_sync_started <= number of peers with sync_started() == true` after every `disconnected()` call. Run a stress test that repeatedly connects and disconnects peers while the sync timer fires concurrently. The invariant will be violated when the race is hit.

### Citations

**File:** sync/src/synchronizer/mod.rs (L670-691)
```rust
        for peer in peers {
            // Only sync with 1 peer if we're in IBD
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
            {
                if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
                    peer_state.start_sync(HeadersSyncController::from_header(&tip));
                }
            }

            debug!("Start sync peer={}", peer);
            active_chain.send_getheaders_to_peer(nc, peer, tip.number_and_hash());
        }
```

**File:** sync/src/types/mod.rs (L107-109)
```rust
    fn started(&self) -> bool {
        matches!(self.headers_sync_state, HeadersSyncState::Started)
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
