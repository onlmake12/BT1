Audit Report

## Title
Incorrect Tracking Variable in `DownloadScheduler::decrease` Bypasses Three-Strike Dampening - (File: sync/src/types/mod.rs)

## Summary
`DownloadScheduler::decrease` at line 522 of `sync/src/types/mod.rs` writes `self.timeout_count = self.task_count.saturating_add(num)` instead of `self.timeout_count.saturating_add(num)`. Because `task_count` is initialized to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` (a value well above 2) and `num` is 1 or 2, the guard `if self.timeout_count > 2` fires unconditionally on every call, decrementing `task_count` immediately rather than only after three accumulated slow-download events as designed.

## Finding Description
`DownloadScheduler` is initialized with `task_count = INIT_BLOCKS_IN_TRANSIT_PER_PEER` and `timeout_count = 0`. The intended design is that `timeout_count` accumulates slow-download events and `task_count` is only decremented after three such events. The buggy implementation:

```rust
// sync/src/types/mod.rs L521-527
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num); // ← task_count, not timeout_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

Since `task_count` starts at `INIT_BLOCKS_IN_TRANSIT_PER_PEER` (≥ 32), adding `num` (1 or 2) always yields a value > 2. The `if`-branch fires on every single call, decrementing `task_count` and resetting `timeout_count` to 0 immediately. The accumulator never accumulates.

The call path is `remove_by_block` (L785–L819) → `decrease(1)` for `NormalToUpper` or `decrease(2)` for `UpperToMax` latency quantiles, gated by `should_punish` (L786, L649). When `task_count` reaches 0, `prune` (L692–L700) removes the peer from `download_schedulers` and adds it to `disconnect_list`.

## Impact Explanation
The three-strike dampening is permanently disabled for all peers. Every slow block download immediately decrements `task_count`, causing peers to reach `task_count == 0` (and be disconnected) three times faster than the design intends. This degrades IBD and steady-state sync throughput by prematurely evicting peers experiencing transient network jitter. This matches **Low (501–2000 points): Any other important performance improvements for CKB**.

## Likelihood Explanation
The code path is exercised during normal block synchronization for every downloaded block whose latency falls in the `NormalToUpper` or `UpperToMax` quantile. No special privileges are required. The only precondition is `should_punish` (requiring `download_schedulers.len() > protect_num`), which is routinely satisfied on a well-connected node. Any peer subject to normal network jitter can trigger this path repeatedly without any attacker involvement.

## Recommendation
Change line 522 from:
```rust
self.timeout_count = self.task_count.saturating_add(num);
```
to:
```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```
This restores the intended three-strike dampening so that `task_count` is only decremented after three accumulated slow-download events.

## Proof of Concept
With `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32` and `num = 1`:

**Buggy behavior:**
- Call 1: `timeout_count = 32 + 1 = 33 > 2` → `task_count = 31`, `timeout_count = 0`
- Call 2: `timeout_count = 31 + 1 = 32 > 2` → `task_count = 30`, `timeout_count = 0`
- `task_count` reaches 0 after 32 slow blocks → peer disconnected.

**Correct behavior:**
- Call 1: `timeout_count = 0 + 1 = 1 ≤ 2` → no change
- Call 2: `timeout_count = 1 + 1 = 2 ≤ 2` → no change
- Call 3: `timeout_count = 2 + 1 = 3 > 2` → `task_count = 31`, `timeout_count = 0`
- `task_count` reaches 0 after 96 slow blocks.

A unit test in `sync/src/tests/inflight_blocks.rs` can call `decrease(1)` three times on a fresh `DownloadScheduler` and assert `task_count` is unchanged after the first two calls and decremented only on the third. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** sync/src/types/mod.rs (L521-527)
```rust
    fn decrease(&mut self, num: usize) {
        self.timeout_count = self.task_count.saturating_add(num);
        if self.timeout_count > 2 {
            self.task_count = self.task_count.saturating_sub(1);
            self.timeout_count = 0;
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
