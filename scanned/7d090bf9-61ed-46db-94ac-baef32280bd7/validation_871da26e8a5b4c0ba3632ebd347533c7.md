Based on my investigation of the CKB codebase, I found a concrete analog to the external report's vulnerability class (insufficient initialization/state checks allowing re-entry that corrupts counters).

---

### Title
`n_sync_started` Counter Corruption via Unchecked Re-entry in `Peers::sync_connected()` — (`File: sync/src/types/mod.rs`)

### Summary

`Peers::sync_connected()` unconditionally resets a peer's `HeadersSyncState` to `SyncProtocolConnected` via `state.sync_connected()` without first checking whether the peer is already in the `Started` state. If the function is called for a peer that is already actively syncing (`headers_sync_state == Started`), the state is silently downgraded to `SyncProtocolConnected` without decrementing the shared `n_sync_started` atomic counter. The subsequent `disconnected()` call then skips the decrement (because `sync_started()` now returns false), permanently inflating `n_sync_started`. During IBD, this permanently blocks all further header synchronization.

### Finding Description

`Peers::sync_connected()` is the handler called when a peer registers on the Sync protocol. It uses a DashMap `entry().and_modify()` pattern to update an already-existing peer entry: [1](#0-0) 

The inner call `state.sync_connected()` unconditionally overwrites `headers_sync_state`: [2](#0-1) 

Which calls: [3](#0-2) 

There is no guard checking the current state before overwriting. Compare this to `suspend_sync()` and `tip_synced()`, which both check `peer_state.sync_started()` before decrementing `n_sync_started`: [4](#0-3) 

The `disconnected()` handler also checks `sync_started()` before decrementing: [5](#0-4) 

The `n_sync_started` counter is incremented in `start_sync_headers()` when a peer transitions to `Started`: [6](#0-5) 

If `sync_connected()` is called on a peer already in `Started` state, the state is reset to `SyncProtocolConnected` without a matching decrement, permanently inflating `n_sync_started`.

Additionally, `sync_connected()` increments `n_protected_outbound_peers` before checking whether the peer already exists in the state map: [7](#0-6) 

If the peer already exists (e.g., inserted by `relay_connected()` first), the counter is incremented again without a corresponding extra decrement on disconnect, inflating `n_protected_outbound_peers` toward `MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT`.

### Impact Explanation

**`n_sync_started` inflation**: During IBD, `start_sync_headers()` enforces `if ibd && x != 0 { None }` — only one sync peer is allowed at a time. A permanently inflated `n_sync_started` (stuck at ≥ 1) means no new header sync can ever start, halting IBD permanently. The node becomes unable to sync the chain.

**`n_protected_outbound_peers` inflation**: The counter reaching `MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT` prevents any future outbound peer from receiving eviction protection, degrading the node's eclipse-attack resistance.

### Likelihood Explanation

The trigger requires `sync_connected()` to be called for a peer that is already in `Started` state. This can occur when:
1. A peer connects to the Relay protocol first (`relay_connected()` inserts the peer with default flags).
2. The peer then connects to the Sync protocol (`sync_connected()` finds the existing entry via `and_modify()`).
3. Header sync starts, incrementing `n_sync_started` and setting state to `Started`.
4. The Sync protocol sub-connection is re-established (e.g., protocol re-negotiation, or a crafted disconnect/reconnect of only the Sync sub-protocol without triggering the full `disconnected()` cleanup) — `sync_connected()` is called again, resetting state without decrementing.

A malicious peer controlling the timing of protocol-level connect/disconnect events can trigger this. The `relay_connected()` path that pre-inserts the peer makes the `and_modify()` branch reachable without a full prior `sync_connected()` call. [8](#0-7) 

### Recommendation

Add a state guard in `sync_connected()` (analogous to the fix recommended in the external report) before overwriting `headers_sync_state`. If the peer is already in `Started` state, decrement `n_sync_started` before resetting, or reject the re-initialization entirely:

```rust
.and_modify(|state| {
    // Guard: if already started, decrement counter before reset
    if state.sync_started() {
        // caller must decrement n_sync_started here
    }
    state.peer_flags = peer_flags;
    state.sync_connected();
})
```

Also, check whether the peer already exists in the state map **before** incrementing `n_protected_outbound_peers`, to prevent counter inflation on re-entry.

### Proof of Concept

1. Attacker peer connects to the local node's Relay protocol → `relay_connected()` inserts peer with `is_protect = false`, `headers_sync_state = Initialized`.
2. Attacker peer connects to the Sync protocol → `sync_connected()` hits `and_modify()`, increments `n_protected_outbound_peers`, sets `is_protect = true`, resets state to `SyncProtocolConnected`.
3. Node calls `start_sync_headers()` for this peer → `n_sync_started` incremented to 1, state set to `Started`.
4. Attacker peer triggers a Sync-protocol-level reconnect (without full peer disconnect) → `sync_connected()` is called again for the same `PeerIndex`, hits `and_modify()`, resets state to `SyncProtocolConnected`. `n_sync_started` remains 1.
5. Full `disconnected()` fires → `sync_started()` returns false (state is `SyncProtocolConnected`) → `n_sync_started` NOT decremented → counter stuck at 1.
6. Node is in IBD. `start_sync_headers()` checks `if ibd && x != 0 { None }` → returns `Err` for every candidate peer → no header sync can ever start → node is permanently stalled. [9](#0-8) [10](#0-9)

### Citations

**File:** sync/src/types/mod.rs (L79-117)
```rust
impl ChainSyncState {
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
}
```

**File:** sync/src/types/mod.rs (L321-323)
```rust
    pub(crate) fn sync_connected(&mut self) {
        self.chain_sync.connected()
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

**File:** sync/src/types/mod.rs (L830-840)
```rust
        let protect_outbound = is_outbound
            && self
                .n_protected_outbound_peers
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |x| {
                    if x < MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT {
                        Some(x + 1)
                    } else {
                        None
                    }
                })
                .is_ok();
```

**File:** sync/src/types/mod.rs (L848-858)
```rust
        self.state
            .entry(peer)
            .and_modify(|state| {
                state.peer_flags = peer_flags;
                state.sync_connected();
            })
            .or_insert_with(|| {
                let mut state = PeerState::new(peer_flags);
                state.sync_connected();
                state
            });
```

**File:** sync/src/types/mod.rs (L861-865)
```rust
    pub fn relay_connected(&self, peer: PeerIndex) {
        self.state
            .entry(peer)
            .or_insert_with(|| PeerState::new(PeerFlags::default()));
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

**File:** sync/src/types/mod.rs (L1410-1419)
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
```

**File:** sync/src/synchronizer/mod.rs (L674-691)
```rust
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
