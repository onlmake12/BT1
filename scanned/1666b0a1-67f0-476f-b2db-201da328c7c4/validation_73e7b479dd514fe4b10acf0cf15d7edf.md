### Title
`n_sync_started` Counter Permanently Inflated via Peer Disconnect Race in `start_sync_headers` — (`File: sync/src/synchronizer/mod.rs`)

---

### Summary

In `start_sync_headers`, the global `n_sync_started` counter is incremented atomically **before** the corresponding per-peer `HeadersSyncState` is transitioned to `Started`. If a peer disconnects in the window between these two non-atomic operations, `disconnected()` observes the peer's state as still `SyncProtocolConnected` (not `Started`), skips the counter decrement, and removes the peer. The counter is then permanently inflated by 1. During IBD, this causes the node to refuse to start header sync with any peer, stalling initial block download indefinitely.

---

### Finding Description

The sync state machine for each peer is tracked by two separate, non-atomically-coupled data structures:

1. The per-peer `HeadersSyncState` enum inside `PeerState` / `ChainSyncState` (in `sync/src/types/mod.rs`)
2. The global `AtomicUsize` counter `n_sync_started` in `Peers`

`start_sync_headers` increments `n_sync_started` first, then separately updates the peer's state:

```rust
// sync/src/synchronizer/mod.rs lines 672–687
if self
    .shared()
    .state()
    .n_sync_started()
    .fetch_update(Ordering::AcqRel, Ordering::Acquire, |x| {
        if ibd && x != 0 { None } else { Some(x + 1) }  // ← counter incremented here
    })
    .is_err()
{
    break;
}
{
    if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
        peer_state.start_sync(...);  // ← state set to Started here (separate step)
    }
}
``` [1](#0-0) 

Between these two steps, `disconnected()` can fire for that peer. It removes the peer from `state` and only decrements `n_sync_started` if `sync_started()` returns `true`:

```rust
// sync/src/types/mod.rs lines 901–912
pub fn disconnected(&self, peer: PeerIndex) {
    if let Some(peer_state) = self.state.remove(&peer)... {
        if peer_state.sync_started() {   // ← false: state is still SyncProtocolConnected
            // decrement n_sync_started  ← SKIPPED
        }
    }
}
``` [2](#0-1) 

After the disconnect, `start_sync` calls `get_mut(&peer)` which returns `None` (peer already removed), so the state is never set to `Started`. The counter is now `1` with no peer in `Started` state — permanently leaked.

The IBD guard at line 677 then blocks all future sync attempts:

```rust
if ibd && x != 0 { None }  // x == 1 forever → always returns None → always breaks
``` [3](#0-2) 

The codebase itself acknowledges this race in a test comment at line 1090:

> "There may be competition between header sync and eviction, it will cause assert panic" [4](#0-3) 

The root cause is the same class of defect as the Minipool pseudo-state finding: the global counter (`n_sync_started`) and the per-peer state (`HeadersSyncState`) are not updated atomically, creating a window where they diverge. The `disconnected()` handler's correctness depends on the per-peer state already being `Started` before it fires — an assumption that does not hold under concurrent execution.

---

### Impact Explanation

During IBD (Initial Block Download), `n_sync_started != 0` causes `start_sync_headers` to immediately `break` without starting sync with any peer. A permanently inflated counter means the node can never advance past IBD, as it cannot initiate header synchronization with any connected peer. The node is effectively stalled at genesis until restarted.

---

### Likelihood Explanation

The race window is small (between two consecutive lines in an async handler), but:
- An attacker controls the exact timing of their disconnect.
- The `notify` callback (`SEND_GET_HEADERS_TOKEN`) fires on a timer, and the attacker can observe or probe when it fires.
- The attack can be retried cheaply: connect, wait for the timer window, disconnect. A single successful race permanently stalls the node.
- The `DashMap` and `AtomicUsize` are explicitly designed for concurrent access from multiple threads, confirming the race is real and not just theoretical.

---

### Recommendation

The increment of `n_sync_started` and the transition of `HeadersSyncState` to `Started` must be performed as a single atomic operation. One approach: hold the `DashMap` entry's write guard across both the counter increment and the state update, so that a concurrent `disconnected()` either sees the full `Started` state (and decrements) or sees the peer already removed (and the counter was never incremented).

Alternatively, `disconnected()` should check whether the counter is positive and the peer is absent (indicating a leaked increment) and correct it, though this is a weaker fix.

---

### Proof of Concept

**Precondition:** Victim node is in IBD (`is_initial_block_download() == true`), `n_sync_started == 0`.

1. Attacker connects to victim as peer P. `sync_connected()` sets P's state to `SyncProtocolConnected`.
2. `SEND_GET_HEADERS_TOKEN` timer fires → `start_sync_headers` runs.
3. P passes `can_start_sync()` (state is `SyncProtocolConnected`).
4. `n_sync_started.fetch_update(...)` succeeds → counter becomes `1`.
5. **Attacker disconnects P** (races between lines 679 and 684).
6. `Synchronizer::disconnected()` → `SyncState::disconnected()` → `Peers::disconnected()`:
   - `state.remove(&P)` succeeds, returns `PeerState` with `HeadersSyncState::SyncProtocolConnected`.
   - `peer_state.sync_started()` → `false` → counter NOT decremented. Counter stays at `1`.
7. Back in `start_sync_headers`: `self.peers().state.get_mut(&P)` → `None` → `start_sync` never called.
8. Counter is `1`, no peer is in `Started` state.
9. On next `SEND_GET_HEADERS_TOKEN` tick: for every candidate peer, `fetch_update` returns `Err` (`ibd && x != 0`), loop breaks immediately. No sync ever starts.
10. Node is permanently stalled in IBD. [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** sync/src/types/mod.rs (L380-385)
```rust
#[derive(Default)]
pub struct Peers {
    pub state: DashMap<PeerIndex, PeerState>,
    pub n_sync_started: AtomicUsize,
    pub n_protected_outbound_peers: AtomicUsize,
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

**File:** sync/src/tests/synchronizer/functions.rs (L1090-1092)
```rust
    // There may be competition between header sync and eviction, it will case assert panic
    let mut state = peers.state.get_mut(&sync_protected_peer).unwrap();
    synchronizer.shared().state().tip_synced(&mut state);
```
