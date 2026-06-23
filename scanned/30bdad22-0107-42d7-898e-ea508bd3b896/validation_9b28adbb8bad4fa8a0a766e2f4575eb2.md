### Title
`n_sync_started` Counter Incremented Before Peer State Committed, Enabling Permanent IBD Header-Sync Stall - (File: `sync/src/synchronizer/mod.rs`)

---

### Summary

In `start_sync_headers`, the global atomic counter `n_sync_started` is incremented **before** the corresponding peer's `HeadersSyncState` is transitioned to `Started`. If the peer disconnects in the narrow window between these two non-atomic operations, `Peers::disconnected` skips the counter decrement (because `sync_started()` still returns `false`), permanently inflating `n_sync_started`. In IBD mode the IBD guard `if ibd && x != 0 { None }` then prevents any subsequent peer from being selected for header sync, stalling the node indefinitely.

---

### Finding Description

`start_sync_headers` in `sync/src/synchronizer/mod.rs` performs two logically coupled but non-atomic operations:

1. Atomically increment `n_sync_started` via `fetch_update`.
2. Separately acquire a `DashMap` write-lock on `peers.state` and call `peer_state.start_sync()` to set `HeadersSyncState::Started`. [1](#0-0) 

Between steps 1 and 2, the peer entry in `peers.state` is still in `SyncProtocolConnected` state, so `sync_started()` returns `false`. [2](#0-1) 

If the peer disconnects in this window, `Peers::disconnected` removes the peer from `peers.state`, checks `peer_state.sync_started()` (which is still `false`), and **does not decrement** `n_sync_started`. [3](#0-2) 

The counter is now permanently 1 with no peer in `Started` state. The subsequent `peer_state.start_sync()` call silently does nothing because `get_mut(&peer)` returns `None` for the already-removed peer. [4](#0-3) 

The only paths that decrement `n_sync_started` are `suspend_sync`, `tip_synced`, and `disconnected`, all of which gate on `sync_started() == true`. [5](#0-4) 

Since no peer ever reaches `Started` state in this scenario, none of these paths fire, and the counter is stuck.

---

### Impact Explanation

In IBD mode, `start_sync_headers` enforces a single-peer constraint:

```rust
if ibd && x != 0 { None } else { Some(x + 1) }
``` [6](#0-5) 

With `n_sync_started` permanently at 1, every subsequent call to `start_sync_headers` during IBD returns `Err` from `fetch_update` and immediately `break`s the loop. No peer is ever selected for header sync again. The node is stuck in IBD and cannot advance its chain tip, constituting a complete denial of service against block synchronization.

---

### Likelihood Explanation

The race window is small (a few instructions between `fetch_update` and `get_mut`), but both operations touch concurrent data structures (`AtomicUsize` and `DashMap`) that are designed for multi-threaded access. The CKB sync protocol runs on a multi-threaded tokio executor, so concurrent execution of `start_sync_headers` (via `SEND_GET_HEADERS_TOKEN` notify) and `disconnected` (via network event) is realistic. An unprivileged peer can connect to the node and immediately close the TCP connection, repeatedly, to probabilistically hit this window. The `SEND_GET_HEADERS_TOKEN` timer fires periodically, giving an attacker repeated attempts. No special privileges, keys, or majority hashpower are required.

---

### Recommendation

Ensure `n_sync_started` is only incremented **after** `peer_state.start_sync()` has been successfully applied. One approach: acquire the `DashMap` write-lock on the peer entry first, verify the peer still exists, call `start_sync()`, and only then increment the counter. Alternatively, hold the peer's `DashMap` guard across both operations so a concurrent `disconnected` call cannot remove the peer between them.

---

### Proof of Concept

1. Node enters IBD. `n_sync_started == 0`.
2. `SEND_GET_HEADERS_TOKEN` fires; `start_sync_headers` iterates eligible peers and selects peer P.
3. `fetch_update` succeeds: `n_sync_started` becomes 1. Peer P's state is still `SyncProtocolConnected`.
4. Attacker (peer P) closes the TCP connection. The network layer fires `disconnected(P)` concurrently.
5. `Peers::disconnected` removes P from `peers.state`; `sync_started()` returns `false`; `n_sync_started` is **not** decremented. `n_sync_started == 1`.
6. `start_sync_headers` attempts `get_mut(&P)` → `None`; `start_sync` is never called.
7. All future `start_sync_headers` calls in IBD mode hit `ibd && x != 0` → `fetch_update` returns `Err` → `break`. No peer is ever synced. Node is permanently stalled in IBD. [7](#0-6) [8](#0-7)

### Citations

**File:** sync/src/synchronizer/mod.rs (L652-692)
```rust
    fn start_sync_headers(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
        let now = unix_time_as_millis();
        let active_chain = self.shared.active_chain();
        let ibd = active_chain.is_initial_block_download();
        let peers: Vec<PeerIndex> = self
            .peers()
            .state
            .iter()
            .filter(|kv_pair| kv_pair.value().can_start_sync(now, ibd))
            .map(|kv_pair| *kv_pair.key())
            .collect();

        if peers.is_empty() {
            return;
        }

        let tip = self.better_tip_header();

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

**File:** sync/src/types/mod.rs (L1410-1430)
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
