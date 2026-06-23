### Title
Stale `n_sync_started` Counter Due to TOCTOU Race Between Peer Disconnect and Header Sync Initiation Causes Permanent IBD Stall - (File: sync/src/synchronizer/mod.rs, sync/src/types/mod.rs)

---

### Summary

In `start_sync_headers`, the global `n_sync_started` atomic counter is incremented **before** the corresponding `PeerState::start_sync()` flag is set on the peer. If a peer disconnects in the narrow window between these two operations, `Peers::disconnected()` observes `sync_started() == false` and does not decrement the counter. In IBD mode, the invariant `n_sync_started == 0` is required before any new header sync can begin. A stuck counter permanently prevents the node from syncing headers, stalling it in IBD indefinitely.

---

### Finding Description

`start_sync_headers` in `sync/src/synchronizer/mod.rs` performs two non-atomic steps for each candidate peer:

```rust
// Step 1: increment counter atomically
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
// Step 2: set the per-peer sync flag (separate, non-atomic with step 1)
{
    if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
        peer_state.start_sync(HeadersSyncController::from_header(&tip));
    }
}
``` [1](#0-0) 

`Peers::disconnected()` decrements `n_sync_started` only when `peer_state.sync_started()` returns `true`:

```rust
pub fn disconnected(&self, peer: PeerIndex) {
    if let Some(peer_state) = self.state.remove(&peer).map(|(_, peer_state)| peer_state) {
        if peer_state.sync_started() {
            assert_ne!(
                self.n_sync_started.fetch_sub(1, Ordering::AcqRel),
                0,
                "n_sync_started overflow when disconnects"
            );
        }
        ...
    }
}
``` [2](#0-1) 

`sync_started()` delegates to `chain_sync.started()`, which is only set to `true` inside `PeerState::start_sync()`: [3](#0-2) 

**Race window**: If a peer disconnects after Step 1 (counter incremented) but before Step 2 (`start_sync` called), `disconnected()` removes the peer from `peers.state`, finds `sync_started() == false`, and skips the decrement. Step 2 then finds `get_mut` returns `None` (peer already removed) and also skips `start_sync`. The counter is permanently stuck at 1.

The IBD guard in `start_sync_headers` enforces strict single-peer sync:

```rust
if ibd && x != 0 { None } else { Some(x + 1) }
``` [4](#0-3) 

With `n_sync_started` stuck at 1, this `fetch_update` will always fail in IBD mode, and no new header sync can ever start. The node is permanently stalled in IBD until restarted.

The `Peers` struct holding the counter: [5](#0-4) 

The `SyncState::disconnected()` path that is missing cleanup of `n_sync_started` for the race case: [6](#0-5) 

The codebase itself acknowledges this class of race in a test comment: `"There may be competition between header sync and eviction, it will case assert panic"`: [7](#0-6) 

---

### Impact Explanation

A CKB node in IBD mode relies on `n_sync_started == 0` to permit header sync with any peer. If the counter is stuck at 1 due to the race, `start_sync_headers` will never successfully start a new sync session. The node cannot download headers, cannot download blocks, and cannot exit IBD. The node is effectively dead from a sync perspective until manually restarted. This is a remote denial-of-service against the node's ability to participate in the network.

---

### Likelihood Explanation

The race window is narrow (between two consecutive lines in an async handler), but:
- `notify` (which calls `start_sync_headers`) and `disconnected` are both async `CKBProtocolHandler` callbacks that can run concurrently in a multi-threaded tokio runtime.
- An attacker controlling a peer can attempt the timing attack repeatedly at low cost: connect, observe the `SEND_GET_HEADERS_TOKEN` notify interval (1 second), and send a TCP RST at the right moment.
- The attack requires no special privileges — any peer that can establish a TCP connection to the victim node can attempt it.
- A single successful race permanently stalls the node without any self-healing mechanism.

---

### Recommendation

Make the counter increment and the `start_sync` flag set atomic with respect to peer removal. One approach: hold the `peers.state` entry lock across both operations, or check after `start_sync` whether the peer was concurrently removed and roll back the counter if so. Alternatively, move the `n_sync_started` increment inside `PeerState::start_sync()` so that the counter is only incremented when the flag is actually set, and ensure `disconnected()` can observe the correct state.

---

### Proof of Concept

1. Victim node V is in IBD mode (`n_sync_started == 0`).
2. Attacker peer A connects to V.
3. V's `SEND_GET_HEADERS_TOKEN` timer fires (every 1 second), calling `start_sync_headers`.
4. V collects A in the `peers` list (A passes `can_start_sync`).
5. V executes `n_sync_started.fetch_update(...)` → counter becomes 1.
6. **At this exact moment**, A sends a TCP RST / closes the connection.
7. V's async runtime schedules `disconnected(A)`: removes A from `peers.state`, checks `sync_started() == false` (Step 2 hasn't run yet), skips decrement. Counter remains 1.
8. V's `start_sync_headers` loop resumes: `peers.state.get_mut(&A)` returns `None`. `start_sync` is never called.
9. All subsequent `SEND_GET_HEADERS_TOKEN` ticks call `start_sync_headers`, but `fetch_update` always fails because `n_sync_started == 1 != 0` in IBD mode.
10. V is permanently stuck in IBD. No headers are downloaded. No blocks are downloaded. Node is non-functional until restarted.

### Citations

**File:** sync/src/synchronizer/mod.rs (L671-687)
```rust
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
```

**File:** sync/src/types/mod.rs (L297-315)
```rust
    pub fn start_sync(&mut self, headers_sync_controller: HeadersSyncController) {
        self.chain_sync.start();
        self.headers_sync_controller = Some(headers_sync_controller);
    }

    fn suspend_sync(&mut self, suspend_time: u64) {
        let now = unix_time_as_millis();
        self.chain_sync.suspend(now + suspend_time);
        self.headers_sync_controller = None;
    }

    fn tip_synced(&mut self) {
        self.chain_sync.tip_synced();
        self.headers_sync_controller = None;
    }

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

**File:** sync/src/tests/synchronizer/functions.rs (L1090-1092)
```rust
    // There may be competition between header sync and eviction, it will case assert panic
    let mut state = peers.state.get_mut(&sync_protected_peer).unwrap();
    synchronizer.shared().state().tip_synced(&mut state);
```
