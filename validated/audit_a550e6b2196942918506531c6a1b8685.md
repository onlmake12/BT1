### Title
Outbound Peer Eviction Timeout Indefinitely Deferred via Incremental Header Difficulty Updates — (`File: sync/src/synchronizer/mod.rs`)

### Summary

In `eviction()` inside `sync/src/synchronizer/mod.rs`, the `chain_sync.timeout` for an outbound peer is reset to `now + CHAIN_SYNC_TIMEOUT` (12 minutes) whenever the peer's `best_known_header.total_difficulty()` is greater than or equal to the previously recorded `state.chain_sync.total_difficulty`. A malicious outbound peer can exploit this by repeatedly sending valid headers with incrementally increasing difficulty — just enough to satisfy the reset condition each cycle — deferring eviction indefinitely without ever syncing to the local tip.

This is a direct structural analog to the stkAave cooldown-extension bug: just as each `claimRewards()` call inside the claim window proportionally extends the `stakerCooldown`, each valid header sent by the peer inside the eviction window resets `chain_sync.timeout` to a full `CHAIN_SYNC_TIMEOUT` in the future, making the eviction condition permanently unreachable.

---

### Finding Description

The `eviction()` function in `sync/src/synchronizer/mod.rs` implements a three-branch state machine for each outbound peer: [1](#0-0) 

**Branch 1** (lines 590–600): If `best_known_header.total_difficulty() >= local_total_difficulty`, the timeout is cleared (peer is caught up).

**Branch 2** (lines 601–614): If `timeout == 0` **OR** `best_known_header.total_difficulty() >= state.chain_sync.total_difficulty`, the timeout is reset to `now + CHAIN_SYNC_TIMEOUT` and `state.chain_sync.total_difficulty` is updated to the current local difficulty.

**Branch 3** (lines 615–641): If `timeout > 0 && now > timeout`, the peer is evicted (or given a final `getheaders` grace period).

The vulnerability is in Branch 2. The reset condition `best_known_header.total_difficulty() >= state.chain_sync.total_difficulty` compares the peer's best known header against the **old** local difficulty (recorded when the timeout was last set), not the **current** local difficulty. As the local chain grows, a gap opens between `state.chain_sync.total_difficulty` (old) and `local_total_difficulty` (current). A peer that sends any valid header with difficulty falling in this gap satisfies the reset condition, causing the timeout to be pushed forward by another full `CHAIN_SYNC_TIMEOUT` (12 minutes). [2](#0-1) 

`best_known_header` is updated via `may_set_best_known_header`, which is called from `insert_valid_header` whenever a valid header is received from the peer: [3](#0-2) 

`insert_valid_header` is called after full header validation, so the peer must supply headers with valid PoW. However, the peer does not need to mine new blocks — it can relay already-mined headers from a fork or from the main chain at a block height whose cumulative difficulty falls between the old and new thresholds. [4](#0-3) 

The relevant constants confirm the window sizes: [5](#0-4) 

---

### Impact Explanation

An attacker operating as an outbound peer can occupy one or more of the local node's outbound connection slots indefinitely without providing useful sync data. CKB nodes maintain a bounded number of outbound connections. By filling those slots with peers that perpetually reset their eviction timers, an attacker can:

1. Prevent the local node from establishing connections to honest, well-synced peers.
2. Degrade or stall the node's block synchronization, particularly during IBD or after a network partition.
3. In combination with other techniques (e.g., eclipse attack), contribute to isolating the node from the honest chain.

The eviction mechanism's purpose — to disconnect peers that are not providing chain progress — is rendered ineffective.

---

### Likelihood Explanation

The attack is reachable by any unprivileged peer that can establish an outbound connection to the victim node. The attacker needs:

- A valid outbound connection (standard P2P).
- Access to valid headers (already mined by the network) whose cumulative difficulty falls between `state.chain_sync.total_difficulty` (old) and `local_total_difficulty` (current). On a live network where the chain grows continuously, this gap always exists and widens over time.
- The ability to send one such header before each 12-minute eviction window expires.

No privileged access, no majority hashpower, and no key material are required. The cost is one valid P2P `SendHeaders` message per 12-minute cycle.

---

### Recommendation

Mirror the fix recommended in the stkAave report: only perform the timeout-resetting state update **outside** the active eviction window. Specifically, the timeout should only be reset when `chain_sync.timeout == 0` (first observation that the peer is behind). Once a timeout is already running, a peer's incremental header progress should **not** reset it to a full `CHAIN_SYNC_TIMEOUT`; at most it should be allowed to clear the timeout entirely (Branch 1) if the peer fully catches up.

Concretely, remove the second disjunct from the Branch 2 condition:

```rust
// Before (vulnerable):
} else if state.chain_sync.timeout == 0
    || (best_known_header.is_some()
        && best_known_header
            .map(|h| h.total_difficulty().clone())
            >= state.chain_sync.total_difficulty)
{
    state.chain_sync.timeout = now + CHAIN_SYNC_TIMEOUT;
    ...

// After (fixed):
} else if state.chain_sync.timeout == 0 {
    state.chain_sync.timeout = now + CHAIN_SYNC_TIMEOUT;
    ...
```

This ensures that once a timeout is set, only full catch-up (Branch 1) can clear it; incremental header progress no longer resets the clock.

---

### Proof of Concept

**Setup**: Local node tip has cumulative difficulty `D_tip`. Attacker connects as a non-protected, non-whitelist outbound peer.

**Step 1**: Attacker's `best_known_header` is `None` or has difficulty `< D_tip`. On the first `eviction()` call, Branch 2 fires (`timeout == 0`):
```
state.chain_sync.timeout = now + 720_000   // 12 minutes
state.chain_sync.total_difficulty = D_tip  // recorded
``` [2](#0-1) 

**Step 2**: The local chain mines new blocks. After some time, `local_total_difficulty = D_tip + delta`.

**Step 3**: Before the 12-minute window expires, the attacker sends a valid `SendHeaders` message containing a header whose cumulative difficulty `D_peer` satisfies:
```
D_tip <= D_peer < D_tip + delta
```
This header passes `insert_valid_header` validation and `may_set_best_known_header` updates `best_known_header` to `D_peer`.

**Step 4**: On the next `eviction()` call:
- Branch 1 check: `D_peer < D_tip + delta` → **false** (peer is still behind current tip).
- Branch 2 check: `D_peer >= state.chain_sync.total_difficulty (= D_tip)` → **true**.
- Result: `state.chain_sync.timeout = now + 720_000` (reset), `state.chain_sync.total_difficulty = D_tip + delta`.

**Step 5**: Repeat from Step 2. The attacker sends one header per 12-minute cycle, each time with difficulty just above the previously recorded threshold. Eviction (Branch 3) is never reached. [6](#0-5)

### Citations

**File:** sync/src/synchronizer/mod.rs (L590-614)
```rust
                if best_known_header
                    .map(|header_index| header_index.total_difficulty().clone())
                    .unwrap_or_default()
                    >= local_total_difficulty
                {
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
```

**File:** sync/src/synchronizer/mod.rs (L615-641)
```rust
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

**File:** sync/src/types/mod.rs (L873-883)
```rust
    pub fn may_set_best_known_header(&self, peer: PeerIndex, header_index: HeaderIndex) {
        if let Some(mut peer_state) = self.state.get_mut(&peer) {
            if let Some(ref known) = peer_state.best_known_header {
                if header_index.is_better_chain(known) {
                    peer_state.best_known_header = Some(header_index);
                }
            } else {
                peer_state.best_known_header = Some(header_index);
            }
        }
    }
```

**File:** sync/src/types/mod.rs (L1094-1132)
```rust
    pub fn insert_valid_header(&self, peer: PeerIndex, header: &core::HeaderView) {
        let tip_number = self.active_chain().tip_number();
        let store_first = tip_number >= header.number();
        // We don't use header#parent_hash clone here because it will hold the arc counter of the SendHeaders message
        // which will cause the 2000 headers to be held in memory for a long time
        let parent_hash = Byte32::from_slice(header.data().raw().parent_hash().as_slice())
            .expect("checked slice length");
        let parent_header_index = self
            .get_header_index_view(&parent_hash, store_first)
            .expect("parent should be verified");
        let mut header_view = HeaderIndexView::new(
            header.hash(),
            header.number(),
            header.epoch(),
            header.timestamp(),
            parent_hash,
            parent_header_index.total_difficulty() + header.difficulty(),
        );

        let snapshot = Arc::clone(&self.shared.snapshot());
        header_view.build_skip(
            tip_number,
            |hash, store_first| self.get_header_index_view(hash, store_first),
            |number, current| {
                // shortcut to return an ancestor block
                if current.number <= snapshot.tip_number() && snapshot.is_main_chain(&current.hash)
                {
                    snapshot
                        .get_block_hash(number)
                        .and_then(|hash| self.get_header_index_view(&hash, true))
                } else {
                    None
                }
            },
        );
        self.shared.header_map().insert(header_view.clone());
        self.state
            .peers()
            .may_set_best_known_header(peer, header_view.as_header_index());
```

**File:** util/constant/src/sync.rs (L37-42)
```rust
/// Chain sync timeout
pub const CHAIN_SYNC_TIMEOUT: u64 = 12 * 60 * 1000; // 12 minutes
/// Suspend sync time
pub const SUSPEND_SYNC_TIME: u64 = 5 * 60 * 1000; // 5 minutes
/// Eviction response time
pub const EVICTION_HEADERS_RESPONSE_TIME: u64 = 120 * 1000; // 2 minutes
```
