Audit Report

## Title
`n_sync_started` Counter Permanently Inflated via Peer Disconnect Race in `start_sync_headers` — (`sync/src/synchronizer/mod.rs`)

## Summary

In `start_sync_headers`, `n_sync_started` is atomically incremented before the peer's state is updated via `start_sync`. If the peer disconnects between the snapshot collection and the `get_mut` call, `start_sync` is never invoked, leaving `sync_started()` permanently `false` for that peer. Because every decrement path guards on `sync_started()`, the counter is permanently inflated. In IBD mode this causes every subsequent `fetch_update` to return `Err`, breaking the sync loop immediately and stalling the node in Initial Block Download indefinitely.

## Finding Description

`start_sync_headers` (sync/src/synchronizer/mod.rs, L656–691) first collects a snapshot of eligible peers into a `Vec<PeerIndex>`, then for each peer atomically increments `n_sync_started` via `fetch_update`, and only afterwards calls `self.peers().state.get_mut(&peer)` to invoke `peer_state.start_sync(...)`. [1](#0-0) 

The `peers.state` field is a `DashMap` (sync/src/types/mod.rs, L382), and the network layer's disconnect callbacks run concurrently. If a peer disconnects after the snapshot is taken but before `get_mut` is reached, `Peers::disconnected` removes the entry from `state` first. Then `get_mut` returns `None`, `start_sync` is never called, and `sync_started()` (which checks `HeadersSyncState::Started`, set only by `ChainSyncState::start()` inside `PeerState::start_sync`) remains `false`. [2](#0-1) [3](#0-2) 

All three decrement sites guard on `sync_started()`:

- `Peers::disconnected` (L901–924): only decrements if `peer_state.sync_started()`.
- `SyncState::suspend_sync` (L1410–1419): only decrements if `peer_state.sync_started()`.
- `SyncState::tip_synced` (L1421–1430): only decrements if `peer_state.sync_started()`. [4](#0-3) [5](#0-4) 

Since `sync_started()` is `false`, none of these paths will ever decrement the counter. The increment at L676–678 is permanent. [6](#0-5) 

## Impact Explanation

`n_sync_started` is the sole IBD single-peer enforcement gate. With `n_sync_started >= 1`, the condition `ibd && x != 0` causes every subsequent `fetch_update` to return `Err`, immediately breaking the loop. The node stops initiating header sync with any peer and remains stuck in IBD indefinitely with no self-healing mechanism. This maps to **High: Vulnerabilities which could easily crash a CKB node** — the node becomes permanently non-functional for block synchronization, unable to advance its chain tip, validate new blocks, or participate in the network until manually restarted.

## Likelihood Explanation

The race window is between two non-atomic operations: the `fetch_update` increment and the subsequent `get_mut` call on a `DashMap` that is concurrently modified by the network disconnect handler. An unprivileged attacker with only TCP access to the node's P2P port can:

1. Connect to the victim node and wait to appear in the `can_start_sync` filter.
2. Disconnect immediately after the `peers` snapshot is taken but before `get_mut` is reached.

The window is microseconds to low milliseconds, but the attacker can repeat the connect/disconnect cycle at high frequency with no special privileges, keys, or hashpower required. The attack is cheap and repeatable.

## Recommendation

Move the counter increment inside the `get_mut` block so it only occurs when `start_sync` is confirmed to execute:

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
        debug!("Start sync peer={}", peer);
        active_chain.send_getheaders_to_peer(nc, peer, tip.number_and_hash());
    }
}
```

This ensures `n_sync_started` is incremented only when `sync_started()` will subsequently return `true`, keeping the counter and peer state consistent.

## Proof of Concept

1. Victim node enters IBD (`is_initial_block_download() == true`).
2. Attacker peer connects; node adds it to `peers.state` with `SyncProtocolConnected` state.
3. `start_sync_headers()` timer fires; attacker peer passes `can_start_sync()` filter and is collected into the `peers` Vec.
4. Attacker peer immediately disconnects; `Peers::disconnected()` removes it from `peers.state` (`sync_started() == false` → no decrement).
5. Loop reaches attacker's `PeerIndex`:
   - `fetch_update` succeeds: `n_sync_started` becomes 1.
   - `get_mut(&peer)` returns `None` (peer already removed).
   - `start_sync()` is never called; `sync_started()` remains `false`.
6. `n_sync_started == 1` permanently.
7. Next `start_sync_headers()` call: `fetch_update` sees `ibd && x != 0` → returns `Err` → breaks immediately for every peer.
8. Node never initiates header sync again; IBD stalls indefinitely until restart.

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

**File:** sync/src/types/mod.rs (L297-300)
```rust
    pub fn start_sync(&mut self, headers_sync_controller: HeadersSyncController) {
        self.chain_sync.start();
        self.headers_sync_controller = Some(headers_sync_controller);
    }
```

**File:** sync/src/types/mod.rs (L313-315)
```rust
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
