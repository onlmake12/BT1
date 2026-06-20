### Title
`n_sync_started` Counter Leak via Non-Atomic Two-Phase State Update Causes IBD Sync Deadlock - (`File: sync/src/synchronizer/mod.rs`)

---

### Summary

In `start_sync_headers`, the global `n_sync_started` atomic counter is incremented **before** the corresponding peer state is transitioned to `Started`. If the peer disconnects in the narrow window between these two operations, the counter is permanently inflated. Because the IBD guard `if ibd && x != 0 { None }` prevents any new sync from starting when `n_sync_started != 0`, the node becomes permanently stuck in IBD with no active sync peer — a state machine deadlock analogous to the GMX `compound_failed` deadlock.

---

### Finding Description

In `sync/src/synchronizer/mod.rs`, `start_sync_headers` performs a two-phase state update:

**Phase 1** — atomically increment `n_sync_started`:
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

**Phase 2** — update the peer's `HeadersSyncState` to `Started`:
```rust
if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
    peer_state.start_sync(HeadersSyncController::from_header(&tip));
}
``` [1](#0-0) 

These two operations are **not atomic**. The `peers` list is collected before the loop, and between the `fetch_update` and the `get_mut`, the peer can disconnect.

When a peer disconnects, `Peers::disconnected()` is called:

```rust
pub fn disconnected(&self, peer: PeerIndex) {
    if let Some(peer_state) = self.state.remove(&peer)... {
        if peer_state.sync_started() {   // only true if state == Started
            self.n_sync_started.fetch_sub(1, Ordering::AcqRel);
        }
    }
}
``` [2](#0-1) 

`sync_started()` returns `true` only when `HeadersSyncState == Started`:

```rust
fn started(&self) -> bool {
    matches!(self.headers_sync_state, HeadersSyncState::Started)
}
``` [3](#0-2) 

**The race**: if the peer disconnects after Phase 1 but before Phase 2, the peer is removed from the state map while still in `SyncProtocolConnected` state. `sync_started()` returns `false`, so `n_sync_started` is **not decremented** in `disconnected()`. Phase 2's `get_mut` then returns `None` (peer already gone), so `start_sync` is never called. The counter is now permanently 1 higher than the number of peers actually in `Started` state. [4](#0-3) 

The `HeadersSyncState` enum has no recovery path from this stuck counter:

```rust
enum HeadersSyncState {
    Initialized,
    SyncProtocolConnected,
    Started,
    Suspend(u64),
    TipSynced(u64),
}
``` [5](#0-4) 

The only places that decrement `n_sync_started` are `suspend_sync`, `tip_synced`, and `disconnected` — all of which require the peer to be in `Started` state first. There is no periodic reset or self-healing mechanism.

---

### Impact Explanation

In IBD mode, the guard `if ibd && x != 0 { None }` causes `fetch_update` to return `Err` whenever `n_sync_started >= 1`, breaking out of the sync loop immediately:

```rust
if ibd && x != 0 { None } else { Some(x + 1) }
``` [6](#0-5) 

With `n_sync_started` stuck at 1 and no peer in `Started` state, `start_sync_headers` will never select a new sync peer. The node remains in IBD indefinitely, unable to download headers or blocks. The only recovery is a node restart (which resets the in-memory counter). A node operator who does not notice the stall will have their node silently fail to sync.

---

### Likelihood Explanation

The race window is small but real: it spans the time between the `fetch_update` and the `get_mut` call, which are two separate operations with no lock held between them. A malicious peer that connects, gets selected for sync (enters `SyncProtocolConnected`), and immediately disconnects can trigger this. The attacker needs no special privileges — any unprivileged P2P peer can connect and disconnect. Repeated attempts increase the probability of hitting the window. During IBD (the most critical sync phase), only one sync peer is allowed, making the impact of a single successful race maximal.

---

### Recommendation

Make the two-phase update atomic by decrementing `n_sync_started` if `get_mut` returns `None`:

```rust
// Phase 1: increment
if self.shared().state().n_sync_started()
    .fetch_update(..., |x| if ibd && x != 0 { None } else { Some(x + 1) })
    .is_err()
{
    break;
}

// Phase 2: update peer state — if peer is gone, roll back the counter
if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
    peer_state.start_sync(HeadersSyncController::from_header(&tip));
} else {
    // Peer disconnected between Phase 1 and Phase 2 — roll back
    self.shared().state().n_sync_started().fetch_sub(1, Ordering::AcqRel);
    continue;
}
```

Alternatively, hold the peer state lock across both operations to eliminate the race window entirely.

---

### Proof of Concept

1. Node A enters IBD mode (`is_initial_block_download() == true`).
2. Malicious peer B connects → `sync_connected()` sets B's state to `SyncProtocolConnected`.
3. `start_sync_headers` runs: B is selected, `n_sync_started` is incremented to 1 (Phase 1 succeeds).
4. Peer B disconnects immediately → `Peers::disconnected()` is called; B's state is `SyncProtocolConnected` (not `Started`), so `sync_started()` is `false`, `n_sync_started` is **not decremented** (stays at 1).
5. Phase 2's `get_mut(&B)` returns `None` (B already removed); `start_sync` is never called.
6. On the next `start_sync_headers` tick, `n_sync_started.fetch_update` sees `x == 1` in IBD mode → returns `Err` → `break`. No new sync peer is selected.
7. Node A is permanently stuck in IBD with `n_sync_started == 1` and zero peers in `Started` state. All subsequent `start_sync_headers` calls are no-ops. The node cannot advance its chain tip until restarted. [7](#0-6) [2](#0-1)

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

**File:** sync/src/types/mod.rs (L80-116)
```rust
    fn can_start_sync(&self, now: u64) -> bool {
        match self.headers_sync_state {
            HeadersSyncState::Initialized => false,
            HeadersSyncState::SyncProtocolConnected => true,
            HeadersSyncState::Started => false,
            HeadersSyncState::Suspend(until) | HeadersSyncState::TipSynced(until) => until < now,
        }
    }

    fn connected(&mut self) {
        self.headers_sync_state = HeadersSyncState::SyncProtocolConnected;
    }

    fn start(&mut self) {
        self.headers_sync_state = HeadersSyncState::Started
    }

    fn suspend(&mut self, until: u64) {
        self.headers_sync_state = HeadersSyncState::Suspend(until)
    }

    fn tip_synced(&mut self) {
        let now = unix_time_as_millis();
        let avg_interval = (MAX_BLOCK_INTERVAL + MIN_BLOCK_INTERVAL) / 2;
        self.headers_sync_state = HeadersSyncState::TipSynced(now + avg_interval * 1000);
    }

    fn started(&self) -> bool {
        matches!(self.headers_sync_state, HeadersSyncState::Started)
    }

    fn started_or_tip_synced(&self) -> bool {
        matches!(
            self.headers_sync_state,
            HeadersSyncState::Started | HeadersSyncState::TipSynced(_)
        )
    }
```

**File:** sync/src/types/mod.rs (L119-127)
```rust
#[derive(Default, Clone, Debug)]
enum HeadersSyncState {
    #[default]
    Initialized,
    SyncProtocolConnected,
    Started,
    Suspend(u64), // suspend headers sync until this timestamp (milliseconds since unix epoch)
    TipSynced(u64), // already synced to the end, not as the sync target for the time being, until the pause time is exceeded
}
```

**File:** sync/src/types/mod.rs (L901-924)
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
    }
```
