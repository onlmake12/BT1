### Title
Implicit, Unenforced State Transitions in Peer `HeadersSyncState` Machine Allow Unexpected Peer State Manipulation — (`sync/src/types/mod.rs`, `sync/src/synchronizer/headers_process.rs`)

---

### Summary

The `HeadersSyncState` machine governing per-peer header synchronization in `sync/src/types/mod.rs` has no enforced transition guards. Any state can transition to any other state unconditionally. Specifically, a remote peer can send an empty `SendHeaders` message at any time — including immediately after connecting, before sync has ever started — and force the local node to call `tip_synced()` on that peer's state, bypassing the `Started` state entirely. This is a direct analog to the Crowdsale implicit-state-transition bug: the state machine has a defined intended sequence, but the code does not enforce it.

---

### Finding Description

`HeadersSyncState` is defined in `sync/src/types/mod.rs` with five states: [1](#0-0) 

The intended progression is:
`Initialized` → `SyncProtocolConnected` → `Started` → `TipSynced` / `Suspend`

The transition methods on `ChainSyncState` are unconditional — they overwrite the current state with no check of what state the machine is currently in: [2](#0-1) 

In `HeadersProcess::execute()`, when a peer sends a `SendHeaders` message with zero headers, `tip_synced()` is called on the peer state with **no guard on the current `HeadersSyncState`**: [3](#0-2) 

The same unconditional call occurs when a non-full batch of headers is received (fewer than `MAX_HEADERS_LEN`): [4](#0-3) 

Compare this to the guarded `suspend_sync` call in `eviction()`, which correctly checks `sync_started()` before transitioning: [5](#0-4) 

The `tip_synced()` path has no equivalent guard. A peer in `SyncProtocolConnected` state (connected but sync never started, so `n_sync_started` was never incremented for it) can send an empty `SendHeaders` and force the transition `SyncProtocolConnected → TipSynced`, skipping `Started` entirely.

The `n_sync_started` counter is incremented in `start_sync_headers` before `start_sync` is called on the peer: [6](#0-5) 

If `SyncShared::state().tip_synced()` decrements `n_sync_started` (the symmetric operation), calling it when the counter was never incremented for this peer causes an `AtomicUsize` underflow, wrapping to `usize::MAX`. In IBD mode, the guard `if ibd && x != 0 { None }` would then permanently block any new peer from being selected for header sync.

---

### Impact Explanation

An attacker-controlled peer that connects and immediately sends an empty `SendHeaders` message causes:

1. **Incorrect state**: The peer is marked `TipSynced` without having synced any headers. It becomes eligible for block fetching (`started_or_tip_synced()` returns `true`) without having contributed to header sync. [7](#0-6) 

2. **Potential `n_sync_started` underflow**: If `SyncShared::state().tip_synced()` decrements the counter (the symmetric counterpart to the increment in `start_sync_headers`), and the counter was never incremented for this peer, the `AtomicUsize` wraps to `usize::MAX`. During IBD, this permanently prevents the node from starting header sync with any peer, stalling chain synchronization entirely. [8](#0-7) 

---

### Likelihood Explanation

The attack requires only that an attacker establish a P2P connection to the target node and send a single `SendHeaders` message with an empty payload. This is trivially achievable by any unprivileged network peer. No special privileges, keys, or majority hashpower are required. The `received` handler processes `SendHeaders` messages from any connected peer without pre-checking the peer's current sync state. [9](#0-8) 

---

### Recommendation

Add an explicit state guard in `HeadersProcess::execute()` before calling `tip_synced()`. The transition to `TipSynced` should only be permitted when the peer is in `Started` state (i.e., sync was actually initiated for this peer). For example:

```rust
if let Some(mut state) = self.synchronizer.peers().state.get_mut(&self.peer) {
    // Only transition to TipSynced if sync was actually started for this peer
    if state.sync_started() {
        self.synchronizer.shared().state().tip_synced(state.value_mut());
    }
}
```

This mirrors the existing guard in `eviction()` that checks `sync_started()` before calling `suspend_sync`. All state transitions in `HeadersSyncState` should be made explicit and enforced at the call site, not left to the caller to handle correctly. [10](#0-9) 

---

### Proof of Concept

1. Attacker establishes a P2P connection to a CKB node running in IBD mode.
2. The node calls `on_connected` → `sync_connected()` → peer state is `SyncProtocolConnected`. `n_sync_started` is still 0 for this peer.
3. Before `start_sync_headers` selects this peer (which would set state to `Started` and increment `n_sync_started`), the attacker sends a `SendHeaders` message with 0 headers.
4. `HeadersProcess::execute()` receives the empty message, enters the `headers.is_empty()` branch, and calls `self.synchronizer.shared().state().tip_synced(state.value_mut())` unconditionally.
5. The peer state transitions `SyncProtocolConnected → TipSynced` without ever passing through `Started`. `n_sync_started` was never incremented for this peer.
6. If `SyncShared::state().tip_synced()` decrements `n_sync_started`, the counter underflows to `usize::MAX`.
7. In `start_sync_headers`, the IBD guard `if ibd && x != 0 { None }` now always fires, and no peer can ever be selected for header sync again — the node is permanently stalled in IBD. [11](#0-10) [12](#0-11)

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

**File:** sync/src/types/mod.rs (L291-315)
```rust
    pub fn can_start_sync(&self, now: u64, ibd: bool) -> bool {
        // only sync with protect/whitelist peer in IBD
        ((self.peer_flags.is_protect || self.peer_flags.is_whitelist) || !ibd)
            && self.chain_sync.can_start_sync(now)
    }

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

**File:** sync/src/synchronizer/headers_process.rs (L111-125)
```rust
        if headers.is_empty() {
            // Empty means that the other peer's tip may be consistent with our own best known,
            // but empty cannot 100% confirm this, so it does not set the other peer's best header
            // to the shared best known.
            // This action means that if the newly connected node has not been sync with headers,
            // it cannot be used as a synchronization node.
            debug!("HeadersProcess is_empty (synchronized)");
            if let Some(mut state) = self.synchronizer.peers().state.get_mut(&self.peer) {
                self.synchronizer
                    .shared()
                    .state()
                    .tip_synced(state.value_mut());
            }
            return Status::ok();
        }
```

**File:** sync/src/synchronizer/headers_process.rs (L183-192)
```rust
        if headers.len() == MAX_HEADERS_LEN {
            let start = headers.last().expect("empty checked").into();
            self.active_chain
                .send_getheaders_to_peer(self.nc, self.peer, start);
        } else if let Some(mut state) = self.synchronizer.peers().state.get_mut(&self.peer) {
            self.synchronizer
                .shared()
                .state()
                .tip_synced(state.value_mut());
        }
```

**File:** sync/src/synchronizer/mod.rs (L402-405)
```rust
            packed::SyncMessageUnionReader::SendHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    HeadersProcess::new(reader, self, peer, &nc).execute()
                })
```

**File:** sync/src/synchronizer/mod.rs (L619-623)
```rust
                    if state.chain_sync.sent_getheaders {
                        if state.peer_flags.is_protect || state.peer_flags.is_whitelist {
                            if state.sync_started() {
                                self.shared().state().suspend_sync(state);
                            }
```

**File:** sync/src/synchronizer/mod.rs (L672-691)
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
            {
                if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
                    peer_state.start_sync(HeadersSyncController::from_header(&tip));
                }
            }

            debug!("Start sync peer={}", peer);
            active_chain.send_getheaders_to_peer(nc, peer, tip.number_and_hash());
        }
```
