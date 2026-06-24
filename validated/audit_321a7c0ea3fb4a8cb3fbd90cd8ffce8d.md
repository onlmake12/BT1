Audit Report

## Title
TOCTOU Race in `start_sync_headers` Permanently Inflates `n_sync_started`, Blocking IBD Sync — (File: sync/src/synchronizer/mod.rs)

## Summary

`start_sync_headers` increments `n_sync_started` atomically but calls `peer_state.start_sync()` in a separate, non-atomic step under a fresh DashMap lock. If a peer disconnects between these two operations, `n_sync_started` is permanently incremented with no compensating decrement, because `Peers::disconnected` only decrements when `sync_started()` is `true` — a flag that is only set by the `start_sync` call that was skipped. In IBD mode, the IBD guard causes every subsequent sync-peer selection to abort immediately, permanently stalling initial block download.

## Finding Description

**Step 1 — peer collection (L656–662):** Eligible peers are collected into a `Vec`, releasing all DashMap read references. Peer X is captured as eligible at this snapshot. [1](#0-0) 

**Step 2 — atomic increment (L672–682):** `n_sync_started` is incremented via `fetch_update`. No peer-state lock is held. In IBD mode, the closure returns `None` (causing `Err`) if `x != 0`, enforcing single-peer sync. [2](#0-1) 

**Step 3 — separate state write (L683–687):** `get_mut(&peer)` acquires a fresh DashMap entry lock. If the peer disconnected between Steps 2 and 3, this returns `None`, `start_sync` is never called, and there is **no `fetch_sub` rollback** in the `None` branch. [3](#0-2) 

**Why `disconnected` does not compensate (L901–912):** `Peers::disconnected` only decrements `n_sync_started` when `peer_state.sync_started()` returns `true`. Since `start_sync` (which calls `chain_sync.start()`) was never reached, `sync_started()` returns `false`, and the decrement is skipped entirely. [4](#0-3) 

**`sync_started()` gate (L313–315):** The flag is only set by `PeerState::start_sync` → `chain_sync.start()`. With `start_sync` never called, this always returns `false` for the disconnected peer. [5](#0-4) 

**`start_sync` sets the flag (L297–300):** Confirms `chain_sync.start()` is the sole setter of the `started` flag. [6](#0-5) 

**IBD lockout:** With `n_sync_started` stuck at 1, every subsequent `fetch_update` call in IBD mode evaluates `ibd && x != 0` as `true`, returns `None`, and the `is_err()` branch triggers `break` — no peer is ever selected again. [7](#0-6) 

**`Peers` struct — unsynchronized state (L380–385):** `n_sync_started` (atomic) and `state` (DashMap) are two independent fields with no shared lock, confirming the non-atomicity. [8](#0-7) 

## Impact Explanation

A node in IBD with `n_sync_started` permanently stuck at 1 can never select a sync peer again. It cannot download headers, cannot advance its chain, and is permanently non-functional for block synchronization. The node process continues running but is effectively dead for its primary purpose. This matches **High: Vulnerabilities which could easily crash a CKB node** — the node is rendered permanently non-functional for its core operation without process termination.

## Likelihood Explanation

Any unprivileged P2P peer can trigger this: connect to the victim node, wait for `SEND_GET_HEADERS_TOKEN` to fire (periodic timer), then disconnect. The race window between `fetch_update` and `get_mut` is narrow but real — network disconnect callbacks fire asynchronously from the network layer concurrently with the sync timer thread. No authentication, funds, or special knowledge of node internals is required. The attack is freely repeatable across restarts, since the attacker can re-trigger the condition on every new IBD attempt.

## Recommendation

The increment of `n_sync_started` and the call to `peer_state.start_sync()` must be performed atomically under the same DashMap entry lock. Acquire the DashMap write entry for the peer first via `state.get_mut(&peer)`, verify the peer is still eligible, then increment `n_sync_started` and call `start_sync` while holding the entry — mirroring the pattern in `Peers::disconnected` which holds the entry while checking `sync_started()`. Alternatively, add an explicit `fetch_sub` rollback immediately after the `None` branch in Step 3 to compensate for the orphaned increment.

## Proof of Concept

1. Start node A in IBD (`is_initial_block_download()` = `true`).
2. Malicious peer B connects; relay sets state to `SyncProtocolConnected`.
3. `SEND_GET_HEADERS_TOKEN` fires → `start_sync_headers` collects peer B as eligible (Step 1).
4. `fetch_update` succeeds → `n_sync_started` = 1 (Step 2).
5. Peer B disconnects → `Peers::disconnected` removes B, checks `sync_started()` = `false`, does **not** decrement `n_sync_started`.
6. `get_mut(&B)` returns `None` → `start_sync` never called, no rollback (Step 3).
7. `n_sync_started` = 1 permanently.
8. All subsequent `SEND_GET_HEADERS_TOKEN` fires: `fetch_update` returns `Err` (IBD + `n_sync_started != 0`) → `break` → no peer selected.
9. Node A is permanently stuck in IBD. Restart and repeat from step 2 to re-trigger.

### Citations

**File:** sync/src/synchronizer/mod.rs (L656-662)
```rust
        let peers: Vec<PeerIndex> = self
            .peers()
            .state
            .iter()
            .filter(|kv_pair| kv_pair.value().can_start_sync(now, ibd))
            .map(|kv_pair| *kv_pair.key())
            .collect();
```

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
