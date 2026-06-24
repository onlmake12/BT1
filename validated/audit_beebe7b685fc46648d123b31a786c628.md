The code at line 522 is confirmed:

Audit Report

## Title
`DownloadScheduler::decrease` Uses `task_count` Instead of `timeout_count` in Accumulation, Breaking Peer Penalty Accounting - (File: sync/src/types/mod.rs)

## Summary
In `sync/src/types/mod.rs` at line 522, `DownloadScheduler::decrease` assigns `self.task_count.saturating_add(num)` to `self.timeout_count` instead of `self.timeout_count.saturating_add(num)`. This discards any previously accumulated value of `timeout_count` on every call, making the intended "accumulate slow-response events before penalizing" threshold non-functional. The result is that `task_count` is either decremented on every single slow response (Case A) or never decremented further once it reaches 1 (Case B).

## Finding Description
`DownloadScheduler` governs how many blocks a node may request in parallel from a single peer. `task_count` is the concurrency budget (initialized to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 16`); `timeout_count` is intended to accumulate slow-response events and only trigger a decrement of `task_count` once it exceeds 2.

The buggy line at `sync/src/types/mod.rs:522`:
```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num); // BUG: should be self.timeout_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

Because `self.task_count` (not `self.timeout_count`) is used as the base, `timeout_count` is always overwritten with `task_count + num`, never accumulating across calls.

**Case A — `task_count > 2`:** Since `task_count` starts at 16 and `num ≥ 1`, `task_count + num` is always `> 2`. The condition is always true, so `task_count` is decremented on every call to `decrease`. The intended 2-event accumulation gate is completely bypassed.

**Case B — `task_count = 1`, `num = 1`:** `1 + 1 = 2`, which is not `> 2`, so `task_count` is never decremented further. A peer penalized to `task_count = 1` can continue delivering slow blocks indefinitely without triggering any additional penalty through this path.

`decrease` is called from `remove_by_block` at lines 801–810 when a received block's elapsed time falls in the `NormalToUpper` or `UpperToMax` quantile — a normal network condition.

## Impact Explanation
The direct, concrete impact is incorrect sync peer penalty accounting: peers with occasional slow responses have their concurrency budget (`task_count`) reduced on every slow block instead of every third, degrading sync throughput from those peers. Conversely, peers already at `task_count = 1` are never further penalized through this path regardless of continued slow delivery, wasting a download slot. This constitutes a meaningful sync performance regression affecting CKB block synchronization efficiency, fitting **Low (501–2000 points): Any other important performance improvements for CKB**.

## Likelihood Explanation
Any connected peer that sends block responses in the `NormalToUpper` or `UpperToMax` time quantile triggers `decrease`. This is a routine network condition requiring no special privileges — only a standard P2P connection. The bug is triggered continuously during normal sync operation whenever peers exhibit variable latency.

## Recommendation
Fix the typo at `sync/src/types/mod.rs:522`:
```rust
// Before (buggy):
self.timeout_count = self.task_count.saturating_add(num);

// After (correct):
self.timeout_count = self.timeout_count.saturating_add(num);
```
This restores the intended behavior: `timeout_count` accumulates across calls, and `task_count` is only decremented after the accumulated count exceeds 2.

## Proof of Concept
Starting from `task_count = 16, timeout_count = 0`, calling `decrease(1)` three times:

| Call | Buggy `timeout_count` | `task_count` after | Correct `timeout_count` | `task_count` after |
|---|---|---|---|---|
| `decrease(1)` | `16+1=17 > 2` → reset | **15** | `0+1=1 ≤ 2` | 16 |
| `decrease(1)` | `15+1=16 > 2` → reset | **14** | `1+1=2 ≤ 2` | 16 |
| `decrease(1)` | `14+1=15 > 2` → reset | **13** | `2+1=3 > 2` → reset | **15** |

With the bug, `task_count` drops by 1 on every call. With the correct code, it drops by 1 only after 3 accumulated slow responses. A unit test can be written directly against `DownloadScheduler::decrease` to assert that after two calls with `num=1` starting from `task_count=16`, `task_count` remains 16, and only after the third call does it become 15. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** sync/src/types/mod.rs (L795-812)
```rust
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
```
