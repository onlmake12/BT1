Audit Report

## Title
Chain-Sync Eviction Timeout Indefinitely Delayed by Incremental Difficulty Announcements - (File: sync/src/synchronizer/mod.rs)

## Summary
The `eviction` function resets the `CHAIN_SYNC_TIMEOUT` deadline whenever a peer's best-known-header total difficulty meets or exceeds `state.chain_sync.total_difficulty`, which is the **previously recorded** local tip difficulty rather than the current local tip difficulty. An attacker who has pre-built a chain with total difficulty near the local tip can repeatedly announce incrementally higher-difficulty headers just before each 12-minute window expires, resetting the deadline indefinitely and permanently occupying an outbound connection slot without ever catching up to the actual local tip.

## Finding Description
In `sync/src/synchronizer/mod.rs`, the `eviction` function at lines 601–614 contains the following reset branch:

```rust
} else if state.chain_sync.timeout == 0
    || (best_known_header.is_some()
        && best_known_header
            .map(|header_index| header_index.total_difficulty().clone())
            >= state.chain_sync.total_difficulty)   // ← previously recorded value
{
    state.chain_sync.timeout = now + CHAIN_SYNC_TIMEOUT;
    state.chain_sync.work_header = Some(tip_header);
    state.chain_sync.total_difficulty = Some(local_total_difficulty); // ← updated to current tip
    state.chain_sync.sent_getheaders = false;
``` [1](#0-0) 

The comparison on line 605 is against `state.chain_sync.total_difficulty`, which was set to the local tip difficulty at the **previous** reset, not the current tip. Because the local chain grows between eviction checks, there is always a window `[D_recorded, D_current_tip)` into which the peer can announce a new best-known header to satisfy the condition and push the deadline forward by another 12 minutes.

The `ChainSyncState` struct holding this mutable deadline is: [2](#0-1) 

The timeout constant is 12 minutes: [3](#0-2) 

The existing guard at lines 590–600 (which clears the timeout when the peer's best-known difficulty reaches the **current** tip) is bypassed because the attacker's chain never reaches the current tip — it only needs to reach the previously recorded value. [4](#0-3) 

## Impact Explanation
By permanently occupying outbound connection slots, an attacker who controls enough peers can fill all outbound slots (typically 8), isolating the victim node from the honest network (eclipse attack). During the eclipse period the attacker can feed the victim a stale or manipulated chain view, enabling double-spend attacks or suppression of transactions. This maps to the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs" and potentially "Vulnerabilities which could easily damage CKB economy."

## Likelihood Explanation
The attacker must supply headers with valid Eaglesong PoW. The initial investment requires pre-building a chain with total cumulative difficulty close to the current local tip — this is a significant but not impossible bar for a well-resourced miner or mining pool. The ongoing cost per reset is only the honest network's block production in one 12-minute window. The attack is reachable by any unprivileged peer discoverable through normal peer-discovery (DNS seeds, peer exchange) with sufficient pre-built chain work. No privileged access, leaked keys, or social engineering is required. Likelihood is **Low-to-Medium** given the substantial initial PoW investment, but the attack is repeatable indefinitely once initiated.

## Recommendation
Change the reset condition to compare the peer's best-known difficulty against the **current** local tip difficulty (`local_total_difficulty`) rather than the previously recorded value (`state.chain_sync.total_difficulty`):

```rust
// Before (vulnerable):
>= state.chain_sync.total_difficulty

// After (fixed):
>= Some(local_total_difficulty.clone())
```

This ensures the timeout is only reset when the peer has genuinely caught up to the current tip, not merely to a stale recorded value. Alternatively, use a monotonically increasing absolute deadline anchored to the first time the peer was observed to be behind, so incremental improvements cannot push the deadline forward. [1](#0-0) 

## Proof of Concept
1. Attacker pre-builds chain `C_attack` with total difficulty `D_attack` slightly below current local tip `D0`.
2. Attacker's node connects to victim as an outbound peer via normal peer discovery.
3. Eviction check at T0: `timeout == 0` fires → `timeout = T0 + 12min`, `recorded = D0`.
4. Honest network mines new blocks; victim tip grows to `D1 > D0`.
5. At T1 = T0 + 11min 59s, attacker mines a few additional blocks on `C_attack` to reach `D_attack' ≥ D0` and sends `SendHeaders` to victim. Victim updates `best_known_header` for this peer.
6. Next eviction tick: condition `D_attack' >= D0` (recorded) is true → `timeout = T1 + 12min`, `recorded = D1`.
7. Attacker extends `C_attack` by enough blocks to reach `D_attack'' ≥ D1` before T1 + 12min expires. Repeats step 5.
8. The peer is never evicted. The outbound slot is permanently occupied.
9. Repeat with multiple attacker-controlled peers to fill all outbound slots and achieve eclipse.

Relevant code path: `eviction` in `sync/src/synchronizer/mod.rs` lines 549–650, reading `ChainSyncState` from `sync/src/types/mod.rs` lines 70–77, with timeout constant from `util/constant/src/sync.rs` line 38. [5](#0-4)

### Citations

**File:** sync/src/synchronizer/mod.rs (L549-650)
```rust
    pub fn eviction(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
        let active_chain = self.shared.active_chain();
        let mut eviction = Vec::new();
        let better_tip_header = self.better_tip_header();
        for mut kv_pair in self.peers().state.iter_mut() {
            let (peer, state) = kv_pair.pair_mut();
            let now = unix_time_as_millis();

            if let Some(ref mut controller) = state.headers_sync_controller {
                let better_tip_ts = better_tip_header.timestamp();
                if let Some(is_timeout) = controller.is_timeout(better_tip_ts, now) {
                    if is_timeout {
                        eviction.push(*peer);
                        continue;
                    }
                } else {
                    active_chain.send_getheaders_to_peer(
                        nc,
                        *peer,
                        better_tip_header.number_and_hash(),
                    );
                }
            }

            // On ibd, node should only have one peer to sync headers, and it's state can control by
            // headers_sync_controller.
            //
            // The header sync of other nodes does not matter in the ibd phase, and parallel synchronization
            // can be enabled by unknown list, so there is no need to repeatedly download headers with
            // multiple nodes at the same time.
            if active_chain.is_initial_block_download() {
                continue;
            }
            if state.peer_flags.is_outbound {
                let best_known_header = state.best_known_header.as_ref();
                let (tip_header, local_total_difficulty) = {
                    (
                        active_chain.tip_header().to_owned(),
                        active_chain.total_difficulty().to_owned(),
                    )
                };
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
            }
        }
        for peer in eviction {
            info!("Timeout eviction peer={}", peer);
            if let Err(err) = nc.disconnect(peer, "sync timeout eviction") {
                debug!("synchronizer disconnect error: {:?}", err);
            }
        }
    }
```

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

**File:** util/constant/src/sync.rs (L37-38)
```rust
/// Chain sync timeout
pub const CHAIN_SYNC_TIMEOUT: u64 = 12 * 60 * 1000; // 12 minutes
```
