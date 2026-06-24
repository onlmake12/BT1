Audit Report

## Title
Remote Peer Can Inflate `task_count` to 128 and Lock 128 Block Slots in `InflightBlocks` for ≥30 Seconds — (`sync/src/types/mod.rs`)

## Summary

An unprivileged remote sync peer can inflate its `DownloadScheduler::task_count` to `MAX_BLOCKS_IN_TRANSIT_PER_PEER=128` by responding to block requests within the `fast_time` threshold, then stop responding. This locks 128 specific `BlockNumberAndHash` entries in `InflightBlocks::inflight_states`, preventing any other peer from being asked for those same blocks. For entries with block numbers beyond `tip + 20`, the `prune` function's iteration bound means the lock persists indefinitely until the tip advances — not merely 30 seconds as the report states, making the actual impact worse than described.

## Finding Description

**Inflation path:**

`remove_by_block` is called each time a block is received from a peer. [1](#0-0) 

When the elapsed response time falls at or below `fast_time` (default **1000 ms**, not 750 ms as stated in the report), `TimeQuantile::MinToFast` is returned and `set.increase(2)` is called with no `should_punish` guard — unlike the `decrease` branches which are gated on `should_punish`. [2](#0-1) 

`increase` unconditionally adds up to `MAX_BLOCKS_IN_TRANSIT_PER_PEER=128`. [3](#0-2) 

Starting from `INIT_BLOCKS_IN_TRANSIT_PER_PEER=32`, only 48 sub-1000ms responses are needed to reach 128. [4](#0-3) 

**Slot locking:**

`InflightBlocks::insert` uses a global `BTreeMap<BlockNumberAndHash, InflightState>`. If a slot is already occupied it returns `false` immediately. [5](#0-4) 

`BlockFetcher::fetch` only adds a block to the fetch list when `insert` returns `true`; a `false` return silently skips the block — no fallback to another peer occurs at that point. [6](#0-5) 

**Timeout — critical `prune` limitation:**

`prune` iterates `inflight_states` (a `BTreeMap` sorted by `BlockNumberAndHash`) and breaks as soon as `key.number > tip + 20`. Entries with block numbers beyond `tip + 20` are **never reached** by the timeout eviction loop, regardless of how long they have been outstanding. [7](#0-6) 

The 30-second eviction (`BLOCK_DOWNLOAD_TIMEOUT`) therefore only applies to entries within `tip + 20`. During IBD, where the victim requests blocks far ahead of the verified tip, the locked slots can persist until the verified tip advances past those block numbers — potentially much longer than 30 seconds. [8](#0-7) 

**`can_fetch` exhaustion:**

With `task_count=128` and 128 hashes in `hashes`, `can_fetch` returns 0, causing `reached_inflight_limit` to return `true` and `BlockFetcher::fetch` to return `None` immediately for that peer. [9](#0-8) [10](#0-9) 

**Existing mitigations are insufficient:**

`remove_by_peer` frees all slots for a peer on disconnect, but the `CHAIN_SYNC_TIMEOUT` is 12 minutes — far longer than the attack window. A peer that stays connected but stops sending blocks is not disconnected within the relevant timeframe. [11](#0-10) [12](#0-11) 

The `should_punish` guard that protects the `decrease`/`punish` branches is absent from the `increase` branch, so even nodes with few peers (where `should_punish=false`) are vulnerable to inflation. [13](#0-12) 

The `time_analyzer` is a single instance shared across all peers inside `InflightBlocks`, so a fast attacker shifts the `fast_time` baseline for all peers. [14](#0-13) 

## Impact Explanation

This matches **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. A single unprivileged peer can stall synchronization of up to 128 specific block slots. For blocks beyond `tip + 20` (common during IBD), the stall is not bounded by the 30-second timeout and persists until the verified tip advances. Multiple coordinated peers can cover disjoint block ranges, amplifying the effect across the entire download window. The victim node's block sync throughput degrades proportionally to the fraction of the download window that is locked.

## Likelihood Explanation

The attacker requires only a standard P2P sync connection and the ability to serve valid blocks within 1000 ms (the actual `fast_time` default — the report incorrectly states 750 ms, but 1000 ms is equally trivial to beat on any reasonable network). No PoW, key material, or privileged access is needed. The 48-response inflation phase is trivially achievable. The attack is repeatable: after `prune` eventually evicts the entries, the attacker can reconnect and repeat.

## Recommendation

1. Apply a `should_punish`-equivalent guard (or a per-peer rate limit) to the `increase` branch in `remove_by_block`, preventing inflation when the peer count is below the protection threshold or when the increase would exceed a per-checkpoint budget.
2. Make `time_analyzer` per-peer rather than a single shared instance inside `InflightBlocks`, so one fast peer cannot shift the quantile thresholds for all peers.
3. Extend `prune` to evict timed-out entries beyond `tip + 20`, or introduce a secondary per-entry absolute deadline that is enforced regardless of block number relative to tip.
4. On detecting a peer with `can_fetch == 0` and no recent block deliveries, proactively re-request its inflight blocks from alternative peers after a shorter interval (e.g., `fast_time * 3`) rather than waiting for the full `BLOCK_DOWNLOAD_TIMEOUT`.

## Proof of Concept

1. Connect a malicious peer; advertise a `best_known_header` ahead of the victim's verified tip by more than 20 blocks (to exercise the `prune` bypass).
2. Respond to every `GetBlocks` message within <1000 ms with valid blocks. After 48 responses, assert `task_count == 128` via the `get_peers` RPC (`can_fetch_count` field).
3. Once the victim has 128 blocks inflight to the malicious peer, stop sending block responses (keep the TCP connection alive).
4. Assert via `get_peers` that `inflight_count == 128` and `can_fetch_count == 0` for the malicious peer.
5. Assert that honest peers are not asked for those same 128 block hashes: `InflightBlocks::insert` returns `false` for each, so `BlockFetcher::fetch` skips them.
6. For entries with block numbers > `tip + 20`: assert they remain in `inflight_states` after 30 seconds have elapsed (i.e., `prune` does not evict them), confirming the indefinite lock.
7. Disconnect the malicious peer; assert that `remove_by_peer` immediately frees all 128 slots and honest peers can resume fetching.

### Citations

**File:** sync/src/types/mod.rs (L504-506)
```rust
    fn can_fetch(&self) -> usize {
        self.task_count.saturating_sub(self.hashes.len())
    }
```

**File:** sync/src/types/mod.rs (L512-519)
```rust
    fn increase(&mut self, num: usize) {
        if self.task_count < MAX_BLOCKS_IN_TRANSIT_PER_PEER {
            self.task_count = ::std::cmp::min(
                self.task_count.saturating_add(num),
                MAX_BLOCKS_IN_TRANSIT_PER_PEER,
            )
        }
    }
```

**File:** sync/src/types/mod.rs (L540-540)
```rust
    time_analyzer: TimeAnalyzer,
```

**File:** sync/src/types/mod.rs (L649-649)
```rust
        let should_punish = self.download_schedulers.len() > self.protect_num;
```

**File:** sync/src/types/mod.rs (L661-686)
```rust
        let end = tip + 20;
        for (key, value) in states.iter() {
            if key.number > end {
                break;
            }
            if value.timestamp + BLOCK_DOWNLOAD_TIMEOUT < now {
                if let Some(set) = download_schedulers.get_mut(&value.peer) {
                    set.hashes.remove(key);
                    if should_punish && adjustment {
                        set.punish(2);
                    }
                };
                if !trace.is_empty() {
                    trace.remove(key);
                }
                remove_key.push(key.clone());
                debug!(
                    "prune: remove InflightState: remove {}-{} from {}",
                    key.number, key.hash, value.peer
                );

                if let Some(metrics) = ckb_metrics::handle() {
                    metrics.ckb_inflight_timeout_count.inc();
                }
            }
        }
```

**File:** sync/src/types/mod.rs (L748-753)
```rust
    pub fn insert(&mut self, peer: PeerIndex, block: BlockNumberAndHash) -> bool {
        let state = self.inflight_states.entry(block.clone());
        match state {
            Entry::Occupied(_entry) => return false,
            Entry::Vacant(entry) => entry.insert(InflightState::new(peer)),
        };
```

**File:** sync/src/types/mod.rs (L766-783)
```rust
    pub fn remove_by_peer(&mut self, peer: PeerIndex) -> usize {
        let trace = &mut self.trace_number;
        let state = &mut self.inflight_states;

        self.download_schedulers
            .remove(&peer)
            .map(|blocks| {
                let blocks_count = blocks.hashes.iter().len();
                for block in blocks.hashes {
                    state.remove(&block);
                    if !trace.is_empty() {
                        trace.remove(&block);
                    }
                }
                blocks_count
            })
            .unwrap_or_default()
    }
```

**File:** sync/src/types/mod.rs (L785-819)
```rust
    pub fn remove_by_block(&mut self, block: BlockNumberAndHash) -> bool {
        let should_punish = self.download_schedulers.len() > self.protect_num;
        let download_schedulers = &mut self.download_schedulers;
        let trace = &mut self.trace_number;
        let time_analyzer = &mut self.time_analyzer;
        let adjustment = self.adjustment;
        self.inflight_states
            .remove(&block)
            .map(|state| {
                let elapsed = unix_time_as_millis().saturating_sub(state.timestamp);
                if let Some(set) = download_schedulers.get_mut(&state.peer) {
                    set.hashes.remove(&block);
                    if adjustment {
                        match time_analyzer.push_time(elapsed) {
                            TimeQuantile::MinToFast => set.increase(2),
                            TimeQuantile::FastToNormal => set.increase(1),
                            TimeQuantile::NormalToUpper => {
                                if should_punish {
                                    set.decrease(1)
                                }
                            }
                            TimeQuantile::UpperToMax => {
                                if should_punish {
                                    set.decrease(2)
                                }
                            }
                        }
                    }
                    if !trace.is_empty() {
                        trace.remove(&block);
                    }
                };
            })
            .is_some()
    }
```

**File:** util/constant/src/sync.rs (L14-16)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
/// Maximum number of download blocks that can be requested at one time
pub const MAX_BLOCKS_IN_TRANSIT_PER_PEER: usize = 128;
```

**File:** util/constant/src/sync.rs (L38-38)
```rust
pub const CHAIN_SYNC_TIMEOUT: u64 = 12 * 60 * 1000; // 12 minutes
```

**File:** util/constant/src/sync.rs (L47-48)
```rust
/// Block download timeout
pub const BLOCK_DOWNLOAD_TIMEOUT: u64 = 30 * 1000; // 30s
```

**File:** sync/src/synchronizer/block_fetcher.rs (L47-52)
```rust
    pub fn reached_inflight_limit(&self) -> bool {
        let inflight = self.sync_shared.state().read_inflight_blocks();

        // Can't download any more from this peer
        inflight.peer_can_fetch_count(self.peer) == 0
    }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L271-284)
```rust
                } else if (matches!(self.ibd, IBDState::In)
                    || state.compare_with_pending_compact(&hash, now))
                    && state
                        .write_inflight_blocks()
                        .insert(self.peer, (header.number(), hash).into())
                {
                    debug!(
                        "block: {}-{} added to inflight, block_status: {:?}",
                        header.number(),
                        header.hash(),
                        status
                    );
                    fetch.push(header)
                }
```
