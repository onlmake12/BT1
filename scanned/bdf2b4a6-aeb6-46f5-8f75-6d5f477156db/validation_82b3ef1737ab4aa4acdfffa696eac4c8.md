### Title
`ChainSyncState.total_difficulty` Not Updated When `timeout` Is Extended in Second Eviction Phase — (`File: sync/src/synchronizer/mod.rs`)

---

### Summary

In `Synchronizer::eviction()`, when the first `CHAIN_SYNC_TIMEOUT` expires and a `GetHeaders` message is sent to give a lagging outbound peer a final chance, `timeout` is extended to `now + EVICTION_HEADERS_RESPONSE_TIME` but the companion fields `work_header` and `total_difficulty` are **not updated** to reflect the current local tip. Because the subsequent eviction-reset branch compares the peer's best-known difficulty against the now-stale `total_difficulty`, a peer that catches up only to the old reference point (not the current tip) silently resets the entire timeout cycle, escaping eviction and receiving a fresh `CHAIN_SYNC_TIMEOUT` window. This is the direct CKB analog of the FairSide M-10 finding: one field in a coupled state tuple is extended while the dependent reference fields are left stale.

---

### Finding Description

`ChainSyncState` holds four tightly coupled fields:

```
timeout           – deadline timestamp
work_header       – local tip header when timeout was first set
total_difficulty  – local total difficulty when timeout was first set
sent_getheaders   – whether the warning GetHeaders was already sent
``` [1](#0-0) 

The `eviction()` function in `sync/src/synchronizer/mod.rs` runs three mutually exclusive branches for each outbound peer:

**Branch A** (lines 595–600): peer has caught up to current tip → clear all four fields.

**Branch B** (lines 601–614): `timeout == 0` **OR** `peer.best_known_difficulty >= state.chain_sync.total_difficulty` → set all four fields to the current tip and a fresh `CHAIN_SYNC_TIMEOUT`. [2](#0-1) 

**Branch C** (lines 615–640): `timeout > 0 && now > timeout` → if `sent_getheaders` is already true, evict; otherwise send `GetHeaders` and extend the deadline:

```rust
state.chain_sync.sent_getheaders = true;
state.chain_sync.timeout = now + EVICTION_HEADERS_RESPONSE_TIME;
// work_header and total_difficulty are NOT updated here
active_chain.send_getheaders_to_peer(nc, *peer,
    state.chain_sync.work_header.as_ref().expect("...").into());
``` [3](#0-2) 

After Branch C fires, `timeout` advances to `now + EVICTION_HEADERS_RESPONSE_TIME`, but `total_difficulty` still holds **D₁** — the local difficulty at the time the *first* timeout was set. If the local chain advances to **D₂ > D₁** during the `EVICTION_HEADERS_RESPONSE_TIME` window, and the peer catches up to **D₁** (but not **D₂**), then on the very next eviction tick:

- Branch A does not fire (`peer.best_known_difficulty < D₂`).
- Branch B **does** fire because `peer.best_known_difficulty >= state.chain_sync.total_difficulty` (= D₁) is now true.
- Branch B resets `timeout = now + CHAIN_SYNC_TIMEOUT`, `work_header = current_tip`, `total_difficulty = D₂`, `sent_getheaders = false`.

The peer escapes eviction and receives a completely fresh `CHAIN_SYNC_TIMEOUT` window, even though it never responded to the `GetHeaders` challenge. [2](#0-1) 

---

### Impact Explanation

An outbound connection slot is permanently occupied by a peer that never provides useful headers. Because CKB limits the number of outbound peers, exhausting these slots degrades the node's ability to sync from honest peers and weakens eclipse-attack resistance. The peer needs only to trickle headers fast enough to match the stale `D₁` reference before the short `EVICTION_HEADERS_RESPONSE_TIME` window closes — it never needs to reach the actual chain tip.

---

### Likelihood Explanation

The precondition — local chain advances during the `EVICTION_HEADERS_RESPONSE_TIME` window — is the normal operating condition of a live node. A deliberately slow peer (or one under an attacker's control) can observe the local tip at the time the first timeout fires (via any public RPC or by watching announced blocks) and then send just enough headers to match that stale reference before the second deadline. No privileged access, no majority hash power, and no Sybil attack is required; a single outbound connection is sufficient.

---

### Recommendation

When Branch C extends `timeout`, also update `work_header` and `total_difficulty` to the current local tip so that the Branch B comparison always uses a fresh reference:

```rust
// Branch C – second-phase timeout
state.chain_sync.sent_getheaders = true;
state.chain_sync.timeout = now + EVICTION_HEADERS_RESPONSE_TIME;
// ADD: refresh the reference point so Branch B cannot fire on a stale baseline
state.chain_sync.work_header = Some(tip_header.clone());
state.chain_sync.total_difficulty = Some(local_total_difficulty.clone());
active_chain.send_getheaders_to_peer(nc, *peer,
    state.chain_sync.work_header.as_ref().expect("...").into());
``` [3](#0-2) 

---

### Proof of Concept

1. Attacker connects as an outbound peer and announces a best-known header with difficulty **D₀ < D₁** (local tip at connection time).
2. `eviction()` fires Branch B: `timeout = T + CHAIN_SYNC_TIMEOUT`, `total_difficulty = D₁`.
3. At `T + CHAIN_SYNC_TIMEOUT + ε`, Branch C fires: `timeout = T + CHAIN_SYNC_TIMEOUT + EVICTION_HEADERS_RESPONSE_TIME`, `total_difficulty` remains **D₁**, `sent_getheaders = true`.
4. Local chain mines new blocks; tip advances to **D₂ > D₁**.
5. Attacker sends headers up to **D₁** (not **D₂**) before the second deadline.
6. Next `eviction()` call: `peer.best_known_difficulty (= D₁) >= state.chain_sync.total_difficulty (= D₁)` → Branch B fires → `timeout = now + CHAIN_SYNC_TIMEOUT`, `sent_getheaders = false`. Peer is not evicted.
7. Repeat from step 3 indefinitely. [4](#0-3) [1](#0-0)

### Citations

**File:** sync/src/types/mod.rs (L70-77)
```rust
#[derive(Clone, Debug, Default)]
pub struct ChainSyncState {
    pub timeout: u64,
    pub work_header: Option<core::HeaderView>,
    pub total_difficulty: Option<U256>,
    pub sent_getheaders: bool,
    headers_sync_state: HeadersSyncState,
}
```

**File:** sync/src/synchronizer/mod.rs (L595-641)
```rust
                    if state.chain_sync.timeout != 0 {
                        state.chain_sync.timeout = 0;
                        state.chain_sync.work_header = None;
                        state.chain_sync.total_difficulty = None;
                        state.chain_sync.sent_getheaders = false;
                    }
                } else if state.chain_sync.timeout == 0
                    || (best_known_header.is_some()
                        && best_known_header
                            .map(|header_index| header_index.total_difficulty().clone())
                            >= state.chain_sync.total_difficulty)
                {
                    // Our best block known by this peer is behind our tip, and we're either noticing
                    // that for the first time, OR this peer was able to catch up to some earlier point
                    // where we checked against our tip.
                    // Either way, set a new timeout based on current tip.
                    state.chain_sync.timeout = now + CHAIN_SYNC_TIMEOUT;
                    state.chain_sync.work_header = Some(tip_header);
                    state.chain_sync.total_difficulty = Some(local_total_difficulty);
                    state.chain_sync.sent_getheaders = false;
                } else if state.chain_sync.timeout > 0 && now > state.chain_sync.timeout {
                    // No evidence yet that our peer has synced to a chain with work equal to that
                    // of our tip, when we first detected it was behind. Send a single getheaders
                    // message to give the peer a chance to update us.
                    if state.chain_sync.sent_getheaders {
                        if state.peer_flags.is_protect || state.peer_flags.is_whitelist {
                            if state.sync_started() {
                                self.shared().state().suspend_sync(state);
                            }
                        } else {
                            eviction.push(*peer);
                        }
                    } else {
                        state.chain_sync.sent_getheaders = true;
                        state.chain_sync.timeout = now + EVICTION_HEADERS_RESPONSE_TIME;
                        active_chain.send_getheaders_to_peer(
                            nc,
                            *peer,
                            state
                                .chain_sync
                                .work_header
                                .as_ref()
                                .expect("work_header be assigned")
                                .into(),
                        );
                    }
                }
```
