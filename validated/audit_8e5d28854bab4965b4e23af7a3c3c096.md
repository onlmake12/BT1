Audit Report

## Title
`DownloadScheduler::decrease` Uses `self.task_count` Instead of `self.timeout_count` as Accumulator Base, Bypassing Grace Period and Causing Premature Peer Disconnection - (File: `sync/src/types/mod.rs`)

## Summary
In `sync/src/types/mod.rs` at line 522, `DownloadScheduler::decrease` assigns `self.timeout_count` using `self.task_count` as the base for `saturating_add`, rather than `self.timeout_count`. Because `task_count` is initialized to 32, the result always exceeds the threshold of 2, causing `task_count` to be decremented on every slow block response instead of every third one. This collapses the intended three-strike grace period and causes peers to be penalized and eventually disconnected approximately 3× faster than the protocol intends.

## Finding Description
`DownloadScheduler` is initialized with `task_count = INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32` and `timeout_count = 0` ( [1](#0-0) ). The `decrease` function at line 521–527 is intended to accumulate `timeout_count` and only decrement `task_count` after more than 2 slow responses:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num);  // BUG: should be self.timeout_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
``` [2](#0-1) 

Since `task_count` starts at 32, `self.task_count.saturating_add(num)` evaluates to at least 33 on the first call. This always exceeds the `> 2` threshold, so `task_count` is decremented and `timeout_count` is reset to 0 on **every** invocation. The accumulator never builds up.

`decrease` is called from `remove_by_block` when a block's elapsed download time falls in the `NormalToUpper` or `UpperToMax` quantile: [3](#0-2) 

When `task_count` reaches 0, the peer is inserted into `disconnect_list` and removed during `prune`: [4](#0-3) 

## Impact Explanation
This is a performance degradation bug affecting sync throughput. Legitimate peers experiencing transient network latency are disconnected after ~32 slow block responses instead of the intended ~96. The node's adaptive download scheduler collapses `task_count` to 0 far faster than designed, causing unnecessary peer churn and reduced block download capacity. This fits the **Low (501–2000 points)** impact class: an important performance regression for CKB sync behavior that is triggered under normal (non-ideal) network conditions without any special attacker capability.

## Likelihood Explanation
The `decrease` path is exercised whenever a block response time falls in the upper two quantiles of the `TimeAnalyzer` distribution — a routine occurrence under any non-ideal network. The bug is active whenever `adjustment = true` (the default) and the node has sync peers. No special attacker capability is required; any peer that responds slowly (due to load, latency, or deliberate throttling) will trigger this path. The bug fires on every production node running this code.

## Recommendation
Change line 522 from:
```rust
self.timeout_count = self.task_count.saturating_add(num);
```
to:
```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```
This restores the intended semantics: `timeout_count` accumulates across slow responses and only triggers a `task_count` decrement after exceeding 2, providing the designed three-strike grace period.

## Proof of Concept
1. `DownloadScheduler` initializes: `task_count = 32`, `timeout_count = 0`.
2. A sync peer responds slowly; `remove_by_block` calls `set.decrease(1)`.
3. Inside `decrease`: `timeout_count = 32.saturating_add(1) = 33 > 2` → `task_count = 31`, `timeout_count = 0`.
4. Next slow response: `timeout_count = 31 + 1 = 32 > 2` → `task_count = 30`, `timeout_count = 0`.
5. After 32 such events: `task_count = 0` → peer inserted into `disconnect_list` and removed.
6. Under the correct implementation, `timeout_count` would need to reach 3 before any decrement, requiring at least 3 slow responses per decrement — approximately 96 slow responses to reach `task_count = 0`.

### Citations

**File:** sync/src/types/mod.rs (L490-496)
```rust
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

**File:** sync/src/types/mod.rs (L798-811)
```rust
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
```
