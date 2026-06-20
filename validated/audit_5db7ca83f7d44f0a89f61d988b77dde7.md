### Title
`n_sync_started` Counter Permanently Inflated When Peer Disconnects Between Atomic Increment and State Update — (`sync/src/synchronizer/mod.rs`)

### Summary

In `start_sync_headers`, the global `n_sync_started` counter is atomically incremented **before** the corresponding `PeerState::start_sync()` is called. If the peer disconnects in the window between these two operations, the counter is incremented but the peer's `sync_started()` flag is never set. All three decrement sites guard on `sync_started()`, so the counter is never decremented. In IBD mode this permanently blocks the node from selecting any new header-sync peer, stalling chain synchronization indefinitely.

---

### Finding Description

`start_sync_headers` in `sync/src/synchronizer/mod.rs` first collects eligible peers, then for each peer:

1. Atomically increments `n_sync_started` via `fetch_update` (lines 672–682).
2. Only then acquires a mutable reference to the peer's state and calls `peer_state.start_sync()` (lines 683–687).

```rust
// Step 1 – counter incremented unconditionally
if self.shared().state().n_sync_started()
    .fetch_update(Ordering::AcqRel, Ordering::Acquire, |x| {
        if ibd && x != 0 { None } else { Some(x + 1) }
    })
    .is_err()
{
    break;
}
// Step 2 – peer state updated only if peer still exists
{
    if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
        peer_state.start_sync(HeadersSyncController::from_header(&tip));
    }
}
``` [1](#0-0) 

`peers.state` is a `DashMap`. Between the `iter()` snapshot (line 656–662) and the `get_mut` call (line 684), the peer can be removed by a concurrent `Peers::disconnected` call. When that happens `get_mut` returns `None`, `start_sync` is never called, and the peer's `headers_sync_state` remains `SyncProtocolConnected` — not `Started`. [2](#0-1) 

Every site that decrements `n_sync_started` first checks `peer_state.sync_started()`:

```rust
// Peers::disconnected
if peer_state.sync_started() {
    self.n_sync_started.fetch_sub(1, Ordering::AcqRel);
}
``` [3](#0-2) 

```rust
// SyncState::suspend_sync / tip_synced
pub(crate) fn suspend_sync(&self, peer_state: &mut PeerState) {
    if peer_state.sync_started() {
        self.peers.n_sync_started.fetch_sub(1, Ordering::AcqRel);
    }
    ...
}
``` [4](#0-3) 

Because `sync_started()` returns `false` (the flag was never set), none of these paths decrement the counter. The counter is permanently inflated by 1 per occurrence.

---

### Impact Explanation

In IBD mode the IBD-single-peer guard is:

```rust
if ibd && x != 0 { None } else { Some(x + 1) }
``` [5](#0-4) 

Once `n_sync_started` is stuck at 1 (or higher), `fetch_update` always returns `Err` in IBD, the loop immediately `break`s, and no new header-sync peer is ever selected. If the current sync peer also disconnects or times out, the node has zero active sync peers and cannot advance its chain tip. The node is permanently stalled in IBD — it cannot verify blocks, relay transactions, or participate in consensus.

---

### Likelihood Explanation

The race window is small (nanoseconds between two `DashMap` operations), but:

- It requires no special privilege — any remote peer that connects and disconnects during the node's IBD phase can trigger it.
- A malicious peer can deliberately connect, wait for the synchronizer tick that calls `start_sync_headers`, and immediately close the TCP connection, repeatedly until the race fires.
- The synchronizer tick runs on a fixed interval, making the timing predictable.
- The bug can also fire naturally under network churn without any attacker.

---

### Recommendation

Increment `n_sync_started` only **after** confirming that `peer_state.start_sync()` was actually called. Move the `fetch_update` inside the `if let Some(mut peer_state)` block, or roll back the increment when `get_mut` returns `None`:

```rust
let incremented = self.shared().state().n_sync_started()
    .fetch_update(...).is_ok();
if !incremented { break; }

let started = if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
    peer_state.start_sync(HeadersSyncController::from_header(&tip));
    true
} else {
    false
};

if !started {
    // Roll back the counter increment
    self.shared().state().n_sync_started().fetch_sub(1, Ordering::AcqRel);
    continue;
}
```

This ensures `n_sync_started` is always in sync with the number of peers whose `sync_started()` flag is actually set.

---

### Proof of Concept

1. Node A starts in IBD (`n_sync_started == 0`).
2. Peer B connects; `can_start_sync` returns `true` for B.
3. Synchronizer tick fires `start_sync_headers`; B is collected in the `peers` list.
4. `n_sync_started.fetch_update` succeeds → counter becomes 1.
5. Peer B disconnects (TCP RST); `Peers::disconnected` is called, removes B from `state`, checks `sync_started()` → `false`, does **not** decrement counter.
6. `self.peers().state.get_mut(&B)` returns `None`; `start_sync` is never called.
7. Counter is now permanently 1 with no peer in `Started` state.
8. On every subsequent `start_sync_headers` tick, `fetch_update` returns `Err` (IBD + x≠0), loop breaks immediately.
9. Node A has no header-sync peer and cannot advance its chain tip — permanently stalled in IBD.

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
