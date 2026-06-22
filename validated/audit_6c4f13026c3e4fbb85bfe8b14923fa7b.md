### Title
`n_sync_started` Counter Permanently Inflated via Peer Disconnect Race in `start_sync_headers` — (File: `sync/src/synchronizer/mod.rs`)

---

### Summary
In `start_sync_headers`, the atomic counter `n_sync_started` is incremented **before** the corresponding peer state is transitioned to `Started`. If a peer disconnects between those two non-atomic steps, the counter is incremented but never decremented, permanently inflating it. In IBD mode this causes the node to believe a header sync is already in progress and refuse to start any new one, stalling the node indefinitely.

---

### Finding Description

`start_sync_headers` collects eligible peers, then for each peer:

1. Atomically increments `n_sync_started` via `fetch_update`
2. **Separately** calls `peer_state.start_sync()` to set the peer's `HeadersSyncState` to `Started` [1](#0-0) 

These two operations are not atomic. Between step 1 and step 2, a concurrent `SessionClose` event can call `Peers::disconnected()`, which removes the peer from `self.state`. When `start_sync_headers` then calls `self.peers().state.get_mut(&peer)`, it returns `None`, so `start_sync` is never called and the peer state remains `SyncProtocolConnected` (not `Started`). [2](#0-1) 

In `disconnected()`, the decrement of `n_sync_started` is guarded by `peer_state.sync_started()`: [3](#0-2) 

`sync_started()` checks whether `headers_sync_state == Started`: [4](#0-3) 

Because `start_sync` was never called, `sync_started()` returns `false`, the decrement is skipped, and `n_sync_started` is permanently +1 with no corresponding peer in `Started` state.

---

### Impact Explanation

`n_sync_started` is the IBD gate. In `start_sync_headers`, the IBD branch refuses to start any new header sync if `n_sync_started != 0`: [5](#0-4) 

A permanently inflated counter means the node will never enter the `fetch_update` success branch during IBD, so `send_getheaders_to_peer` is never called again. The node is stuck in Initial Block Download indefinitely until restarted. This is a **denial-of-sync** against the local node, triggered by a remote peer.

---

### Likelihood Explanation

The race window is small (between `fetch_update` and `get_mut`), but:
- A malicious peer can connect and immediately disconnect in a tight loop, maximizing the probability of hitting the window.
- No authentication or privilege is required — any inbound or outbound peer can trigger this.
- The node's periodic `start_sync_headers` timer (called from the sync poll loop) runs continuously, so the attacker has repeated opportunities.
- The code itself acknowledges the race: a test comment at line 1090 reads *"There may be competition between header sync and eviction, it will cause assert panic"*. [6](#0-5) 

---

### Recommendation

Increment `n_sync_started` only **after** successfully setting the peer state to `Started`, or use a single lock-protected transaction that updates both atomically. Alternatively, roll back the counter increment if `get_mut` returns `None`:

```rust
// After fetch_update succeeds:
let started = if let Some(mut peer_state) = self.peers().state.get_mut(&peer) {
    peer_state.start_sync(HeadersSyncController::from_header(&tip));
    true
} else {
    false
};

if !started {
    // Roll back the counter increment
    self.shared().state().n_sync_started()
        .fetch_sub(1, Ordering::AcqRel);
    continue;
}
```

---

### Proof of Concept

1. Attacker peer connects → `sync_connected` is called, peer enters `SyncProtocolConnected` state.
2. Node's `start_sync_headers` timer fires; peer passes `can_start_sync` filter and is added to the `peers` list.
3. `n_sync_started.fetch_update` succeeds → counter becomes 1.
4. **Race**: attacker peer disconnects → `Peers::disconnected` removes peer from `self.state`; since `sync_started()` is `false`, counter is NOT decremented.
5. `self.peers().state.get_mut(&peer)` returns `None`; `start_sync` is never called.
6. `n_sync_started` is now permanently 1 with no peer in `Started` state.
7. Next `start_sync_headers` call in IBD: `fetch_update` closure sees `x == 1`, returns `None`, loop breaks immediately — no header sync is ever started again.
8. Node is stuck in IBD until restarted. [7](#0-6) [2](#0-1)

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
