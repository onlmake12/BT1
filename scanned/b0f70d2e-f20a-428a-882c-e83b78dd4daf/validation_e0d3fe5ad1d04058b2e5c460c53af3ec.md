The code is confirmed. Let me verify the `should_punish` condition as well.

Audit Report

## Title
`DownloadScheduler::decrease()` Uses `task_count` Instead of `timeout_count` for Accumulation, Bypassing Slow-Response Guard — (File: sync/src/types/mod.rs)

## Summary
In `sync/src/types/mod.rs` at line 522, `decrease()` assigns `self.timeout_count = self.task_count.saturating_add(num)` instead of `self.timeout_count.saturating_add(num)`. Because `task_count` initializes to 32 (`INIT_BLOCKS_IN_TRANSIT_PER_PEER`), the result is always ≥ 33, which is always `> 2`, so `task_count` is decremented on every single call to `decrease()` rather than only after accumulating 2 slow-response events. This causes sync peers to be disconnected 3× faster than intended under slow-response conditions.

## Finding Description
`DownloadScheduler` manages per-peer block download concurrency. `task_count` is the concurrency budget; `timeout_count` is meant to accumulate slow-response events and only trigger a penalty after exceeding 2.

The confirmed buggy code at line 521–527:
```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num); // BUG: reads task_count, not timeout_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
``` [1](#0-0) 

Since `task_count` starts at 32 (`INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`), `self.task_count.saturating_add(num)` evaluates to ≥ 33 on every call, making `timeout_count > 2` unconditionally true. The intended accumulation guard is completely bypassed: `task_count` is decremented by 1 on every invocation and `timeout_count` is immediately reset to 0. [2](#0-1) [3](#0-2) 

`decrease()` is called from `remove_by_block()` when `should_punish` is true (i.e., `download_schedulers.len() > protect_num`, a common condition with multiple peers): [4](#0-3) [5](#0-4) 

When `task_count` reaches 0, `prune()` disconnects the peer: [6](#0-5) 

## Impact Explanation
This is a sync performance degradation bug. A peer whose block responses fall in the `NormalToUpper` quantile will cause the local node to exhaust its `task_count` after 32 slow responses (instead of the intended ~96), triggering premature disconnection. This degrades block sync throughput under variable-latency network conditions. The impact matches **Low (501–2000 points): Any other important performance improvements for CKB** — it does not crash the node, cause consensus deviation, or damage the economy, but it meaningfully degrades sync behavior in real-world conditions.

## Likelihood Explanation
The `should_punish` condition (`download_schedulers.len() > protect_num`) is satisfied whenever more than `protect_num` (default 4) download peers are connected, which is the normal operating state during IBD and steady-state sync. Variable network latency routinely places block responses in the `NormalToUpper` quantile. No special attacker capability is required; the bug fires under ordinary network conditions. A malicious peer can also deliberately send blocks at slightly elevated latency to accelerate `task_count` depletion and force disconnection.

## Recommendation
Fix line 522 to accumulate into `self.timeout_count` rather than overwrite it with `self.task_count`:
```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.timeout_count.saturating_add(num); // fixed
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

## Proof of Concept
**Initial state**: `task_count = 32`, `timeout_count = 0`, `download_schedulers.len() > protect_num` (should_punish = true).

**Step 1**: Block arrives in `NormalToUpper` quantile → `decrease(1)` called.
- Buggy: `timeout_count = 32 + 1 = 33 > 2` → `task_count = 31`, `timeout_count = 0`
- Correct: `timeout_count = 0 + 1 = 1 ≤ 2` → no penalty

**Step 2**: Another slow block → `decrease(1)` again.
- Buggy: `timeout_count = 31 + 1 = 32 > 2` → `task_count = 30`, `timeout_count = 0`
- Correct: `timeout_count = 1 + 1 = 2 ≤ 2` → still no penalty

**After 32 slow responses**:
- Buggy: `task_count = 0` → `prune()` disconnects the peer
- Correct: `task_count = 31` (only penalized after every 3 slow responses for `decrease(1)`)

A unit test can confirm this by constructing a `DownloadScheduler` with default values, calling `decrease(1)` 32 times, and asserting `task_count == 0` under the buggy code vs. `task_count > 0` under the fix.

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

**File:** sync/src/types/mod.rs (L786-786)
```rust
        let should_punish = self.download_schedulers.len() > self.protect_num;
```

**File:** sync/src/types/mod.rs (L801-810)
```rust
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
```

**File:** util/constant/src/sync.rs (L14-14)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
```
