### Title
TOCTOU Race in `start_sync_headers` Permanently Inflates `n_sync_started`, Blocking IBD Header Sync — (File: `sync/src/synchronizer/mod.rs`)

---

### Summary

In `start_sync_headers`, the `n_sync_started` atomic counter is incremented **before** the corresponding peer state is updated via `peer_state.start_sync()`. If a peer disconnects in the narrow window between those two operations, `n_sync_started` is permanently inflated with no corresponding decrement. In IBD mode, this prevents any new peer from ever starting header sync, permanently stalling the node's initial block download until restart.

---

### Finding Description

In `sync/src/synchronizer/mod.rs`, `start_sync_headers` performs two non-atomic operations in sequence:

**Step 1** — atomically increment `n_sync_started`: [1](#0-0) 

**Step 2** — separately update peer state (not atomic with Step 1): [2](#0-1) 

If the peer disconnects between Step 1 and Step 2, `Peers::disconnected()` is called: [3](#0-2) 

At that moment, `peer_state.sync_started()` is still `false` (because `start_sync` was never called), so `n_sync_started` is **not decremented** in `disconnected()`. Back in `start_sync_headers`, `self.peers().state.get_mut(&peer)` returns `None` (peer already removed), so `peer_state.start_sync()` is never called. The counter is permanently inflated by 1.

Every decrement path — `disconnected`, `suspend_sync`, and `tip_synced` — guards the decrement with `if peer_state.sync_started()`: [4](#0-3) 

Since no peer ever has `sync_started() == true` for this phantom increment, `n_sync_started` is never decremented.

The `Peers` struct holding the counter: [5](#0-4) 

---

### Impact Explanation

In IBD mode, `start_sync_headers` enforces a single-peer limit using `n_sync_started`: [6](#0-5) 

With `n_sync_started` permanently at 1, the condition `if ibd && x != 0 { None }` causes every subsequent `fetch_update` to return `Err`, breaking out of the loop. No new peer can ever start header sync in IBD mode. The node is permanently stuck in IBD — unable to download or verify any new blocks — until it restarts. This is a complete denial of sync service for a bootstrapping node.

---

### Likelihood Explanation

An unprivileged remote peer can trigger this:
1. Connect to a victim node that is in IBD mode.
2. Wait for the periodic `start_sync_headers` call (fired on the sync timer loop).
3. Disconnect at the precise moment after `fetch_update` increments the counter but before `get_mut` is called.

The timing window is narrow (nanoseconds between two sequential lines), making reliable single-attempt exploitation difficult. However, the attacker can reconnect and retry repeatedly at low cost. A single successful race permanently stalls the node's IBD sync with no recovery path short of a restart.

---

### Recommendation

Compensate for the increment if `get_mut` returns `None` — i.e., if the peer no longer exists when `start_sync` is attempted, immediately decrement `n_sync_started` to undo the orphaned increment:

```rust
if self.shared().state().n_sync_started()
    .fetch_update(Ordering::AcqRel, Ordering::Acquire, |x| {
        if ibd && x != 0 { None } else { Some(x + 1) }
    })
    .is_err()
{
    break;
}
let started = if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
    peer_state.start_sync(HeadersSyncController::from_header(&tip));
    true
} else {
    false
};
if !started {
    // Peer disappeared between counter increment and state update; undo the increment.
    self.shared().state().n_sync_started().fetch_sub(1, Ordering::AcqRel);
    continue;
}
```

---

### Proof of Concept

1. Node A is in IBD mode; `n_sync_started == 0`.
2. Attacker connects to Node A; attacker's `PeerIndex` is added to `peers.state`.
3. `start_sync_headers` fires; `peers` list includes the attacker's peer.
4. `n_sync_started.fetch_update(...)` succeeds → `n_sync_started` becomes **1**.
5. Attacker disconnects: `SyncState::disconnected()` → `Peers::disconnected()` is called.
   - `self.state.remove(&attacker_peer)` removes the peer.
   - `peer_state.sync_started()` is `false` → `n_sync_started` is **not decremented**.
6. `self.peers().state.get_mut(&attacker_peer)` returns `None`.
7. `peer_state.start_sync()` is never called.
8. `n_sync_started` is permanently **1** with no peer holding `sync_started == true`.
9. All future `start_sync_headers` calls in IBD mode hit `if ibd && x != 0 { None }` → `Err` → `break`.
10. Node A cannot start header sync with any peer and is permanently stuck in IBD until restart.

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

**File:** sync/src/types/mod.rs (L380-385)
```rust
#[derive(Default)]
pub struct Peers {
    pub state: DashMap<PeerIndex, PeerState>,
    pub n_sync_started: AtomicUsize,
    pub n_protected_outbound_peers: AtomicUsize,
}
```

**File:** sync/src/types/mod.rs (L901-923)
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

            // Protection node disconnected
            if peer_state.peer_flags.is_protect {
                assert_ne!(
                    self.n_protected_outbound_peers
                        .fetch_sub(1, Ordering::AcqRel),
                    0,
                    "n_protected_outbound_peers overflow when disconnects"
                );
            }
        }
```

**File:** sync/src/types/mod.rs (L1410-1429)
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
```
