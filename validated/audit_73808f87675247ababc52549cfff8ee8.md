### Title
IBD Sync Guard (`n_sync_started`) Permanently Stuck via Peer-Disconnect Race — (`sync/src/synchronizer/mod.rs`)

---

### Summary

The `n_sync_started` atomic counter acts as the sole guard preventing more than one peer from driving header sync during Initial Block Download (IBD). A race condition between the atomic increment of this counter and the subsequent `peer_state.start_sync()` call allows the counter to be permanently incremented without a corresponding decrement. Once stuck at 1, the IBD guard never clears, and the node can never start a new sync session — leaving it permanently frozen in IBD mode.

---

### Finding Description

**The guard and its intended invariant**

In `start_sync_headers` (`sync/src/synchronizer/mod.rs:672–691`), the IBD guard is enforced in two separate, non-atomic steps:

```
Step 1 — atomic increment (the "guard"):
    n_sync_started.fetch_update(|x| if ibd && x != 0 { None } else { Some(x+1) })

Step 2 — mark peer state (separate, non-atomic):
    peer_state.start_sync(...)
``` [1](#0-0) 

The counter is decremented in `Peers::disconnected` **only if** `peer_state.sync_started()` returns `true`:

```rust
if peer_state.sync_started() {
    self.n_sync_started.fetch_sub(1, Ordering::AcqRel);
}
``` [2](#0-1) 

**The race**

Because Step 1 and Step 2 are not atomic, the following interleaving is possible:

| Time | Synchronizer thread | Network/disconnect thread |
|------|---------------------|--------------------------|
| T1 | `fetch_update` → `n_sync_started` becomes **1** | |
| T2 | | Peer disconnects → `disconnected()` called |
| T3 | | `peer_state.sync_started()` → **false** (start_sync never ran) |
| T4 | | `n_sync_started` is **NOT decremented** |
| T5 | `state.get_mut(&peer)` → **None** (peer already removed) | |
| T6 | `start_sync()` is **never called** | |

After T6, `n_sync_started == 1` permanently. No peer has `sync_started == true`, so `suspend_sync`, `tip_synced`, and `disconnected` will never decrement it. [3](#0-2) [4](#0-3) 

**Why the guard is now permanently active**

On every subsequent call to `start_sync_headers` in IBD mode, the guard fires:

```rust
if ibd && x != 0 { None }  // returns None → fetch_update returns Err → break
``` [5](#0-4) 

No new sync session can ever be started. The node cannot advance its chain tip, cannot exit IBD mode, and is permanently stalled.

---

### Impact Explanation

- **Severity: High.** A node frozen in IBD mode cannot validate new blocks, cannot relay transactions, and cannot participate in consensus. It is effectively a dead node from the network's perspective.
- The condition is **permanent** until the node process is restarted; there is no self-healing path because the node cannot exit IBD without syncing, and it cannot sync because the guard is stuck.
- The `n_sync_started` counter has no timeout or watchdog reset.

---

### Likelihood Explanation

- **Likelihood: Low-Medium.** The race window (between `fetch_update` and `get_mut`) is narrow (nanoseconds to low microseconds on the same machine). However:
  - An unprivileged remote peer controls the exact timing of its TCP disconnect/RST.
  - `start_sync_headers` is called on a periodic timer, giving the attacker a predictable trigger point.
  - The attacker can reconnect and retry indefinitely at zero cost, making probabilistic exploitation feasible over many attempts.
  - No authentication or rate-limiting prevents repeated connect/disconnect cycles.

---

### Recommendation

Atomically combine the counter increment and the peer-state update. One approach: move `peer_state.start_sync()` **before** `fetch_update`, or use a single lock that covers both operations. Alternatively, add a watchdog that resets `n_sync_started` to 0 whenever no peer has `sync_started == true` and the counter is non-zero:

```rust
// Recovery: if counter > 0 but no peer is actually syncing, reset it
if n_sync_started.load(Ordering::Acquire) > 0
    && !peers.state.iter().any(|s| s.sync_started())
{
    n_sync_started.store(0, Ordering::Release);
}
```

---

### Proof of Concept

1. Attacker peer connects to a CKB node that is in IBD mode (`n_sync_started == 0`).
2. The synchronizer's periodic timer fires `start_sync_headers`; the attacker's peer is selected.
3. `fetch_update` increments `n_sync_started` to 1.
4. Attacker immediately sends TCP RST / closes connection.
5. The network layer calls `Peers::disconnected(peer)` concurrently; `peer_state.sync_started()` is `false` because `start_sync()` has not yet executed; `n_sync_started` is not decremented.
6. `start_sync_headers` calls `state.get_mut(&peer)` → `None`; `start_sync()` is skipped.
7. `n_sync_started` is now permanently 1.
8. All future calls to `start_sync_headers` in IBD mode hit `if ibd && x != 0 { None }` and break immediately.
9. The node never advances its chain tip and never exits IBD mode.

### Citations

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
