Audit Report

## Title
`InflightBlocks::prune` Skips Timeout Eviction for Inflight Entries Above `tip+20`, Permanently Consuming Peer Slots — (`sync/src/types/mod.rs`)

## Summary

`InflightBlocks::prune` hard-stops iteration at `tip + 20`, leaving all inflight entries with `key.number > tip + 20` unvisited on every prune cycle. The secondary cleanup path (`mark_slow_block` / `trace.retain`) only covers entries at `key.number <= tip + 1`. Since `BlockFetcher` can insert entries up to `tip + BLOCK_DOWNLOAD_WINDOW` (`tip + 8192`), a malicious peer that silently drops `GetBlocks` requests for blocks in the range `(tip+20, tip+8192]` will have its inflight slots permanently occupied, its `task_count` never decremented, and will never be punished or disconnected through the `InflightBlocks` mechanism.

## Finding Description

**`prune` hard-stops at `tip + 20`** — `sync/src/types/mod.rs` lines 661–665:
```rust
let end = tip + 20;
for (key, value) in states.iter() {
    if key.number > end {
        break;   // entries above tip+20 are never visited
    }
    if value.timestamp + BLOCK_DOWNLOAD_TIMEOUT < now { ... punish, remove ... }
}
``` [1](#0-0) 

**`mark_slow_block` hard-stops at `tip + 1`** — lines 628–636:
```rust
pub fn mark_slow_block(&mut self, tip: BlockNumber) {
    for key in self.inflight_states.keys() {
        if key.number > tip + 1 { break; }
        self.trace_number.entry(key.clone()).or_insert(now);
    }
}
``` [2](#0-1) 

The `trace.retain` path (lines 713–742) only evicts entries that were previously added to `trace_number` by `mark_slow_block`, so it also covers at most `<= tip + 1`. [3](#0-2) 

**Gap**: entries in `(tip+1, tip+20]` are covered by the `prune` timeout loop; entries in `(tip+20, tip+8192]` are covered by neither path.

**`BlockFetcher` can insert up to `tip + 8192`** — `sync/src/synchronizer/block_fetcher.rs` lines 212–215:
```rust
let mut end = min(fetch_end, window_end(start, BLOCK_DOWNLOAD_WINDOW, best_known.number()));
```
with `BLOCK_DOWNLOAD_WINDOW = 1024 * 8 = 8192` (`util/constant/src/sync.rs` line 54). [4](#0-3) [5](#0-4) 

**Peer is never punished or disconnected**: `punish` is only called inside the `tip+20` loop. The `download_schedulers.retain` eviction check fires only when `task_count == 0` (lines 692–700), but `task_count` is never decremented for entries above `tip+20`, so it stays at its initial value of `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`. [6](#0-5) [7](#0-6) 

**`can_fetch` is permanently zero**: `can_fetch = task_count.saturating_sub(hashes.len())`. With 32 entries inflight and `task_count = 32`, `can_fetch = 0`. Since `hashes` is never cleaned up (no prune path fires), the peer is permanently excluded from further block downloads. [8](#0-7) 

**`inflight_states` blocks other peers from fetching the same blocks**: `insert` returns `false` for any `BlockNumberAndHash` already present, regardless of which peer holds it. So while the malicious peer's entries at `tip+21` to `tip+52` remain, no other peer can be scheduled to fetch those blocks. [9](#0-8) 

## Impact Explanation

A malicious peer can occupy its full 32 inflight slots with entries at block numbers `> tip+20`. Those entries are never evicted on timeout, the peer is never punished, and no other peer can be scheduled to fetch the same blocks. The chain stalls at `tip+20` until the entries age into the prune window as the tip advances — but the

### Citations

**File:** sync/src/types/mod.rs (L489-496)
```rust
impl Default for DownloadScheduler {
    fn default() -> Self {
        Self {
            hashes: HashSet::default(),
            task_count: INIT_BLOCKS_IN_TRANSIT_PER_PEER,
            timeout_count: 0,
        }
    }
```

**File:** sync/src/types/mod.rs (L504-506)
```rust
    fn can_fetch(&self) -> usize {
        self.task_count.saturating_sub(self.hashes.len())
    }
```

**File:** sync/src/types/mod.rs (L628-636)
```rust
    pub fn mark_slow_block(&mut self, tip: BlockNumber) {
        let now = ckb_systemtime::unix_time_as_millis();
        for key in self.inflight_states.keys() {
            if key.number > tip + 1 {
                break;
            }
            self.trace_number.entry(key.clone()).or_insert(now);
        }
    }
```

**File:** sync/src/types/mod.rs (L661-685)
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
```

**File:** sync/src/types/mod.rs (L692-700)
```rust
        download_schedulers.retain(|k, v| {
            // task number zero means this peer's response is very slow
            if v.task_count == 0 {
                disconnect_list.insert(*k);
                false
            } else {
                true
            }
        });
```

**File:** sync/src/types/mod.rs (L713-742)
```rust
        trace.retain(|key, time| {
            // In the normal state, trace will always empty
            //
            // When the inflight request reaches the checkpoint(inflight > tip + 512),
            // it means that there is an anomaly in the sync less than tip + 1, i.e. some nodes are stuck,
            // at which point it will be recorded as the timestamp at that time.
            //
            // If the time exceeds low time limit, delete the task and halve the number of
            // executable tasks for the corresponding node
            if now > timeout_limit + *time {
                if let Some(state) = states.remove(key)
                    && let Some(d) = download_schedulers.get_mut(&state.peer)
                {
                    if should_punish && adjustment {
                        d.punish(1);
                    }
                    d.hashes.remove(key);
                    debug!(
                        "prune: remove download_schedulers: remove {}-{} from {}",
                        key.number, key.hash, state.peer
                    );
                };

                if key.number > *restart_number {
                    *restart_number = key.number;
                }
                return false;
            }
            true
        });
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

**File:** sync/src/synchronizer/block_fetcher.rs (L212-215)
```rust
        let mut end = min(
            fetch_end,
            window_end(start, BLOCK_DOWNLOAD_WINDOW, best_known.number()),
        );
```

**File:** util/constant/src/sync.rs (L54-54)
```rust
pub const BLOCK_DOWNLOAD_WINDOW: u64 = 1024 * 8; // 1024 * default_outbound_peers
```
