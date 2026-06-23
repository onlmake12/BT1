### Title
`DownloadScheduler::decrease` Uses Wrong Base Variable, Bypassing Timeout Grace Period and Causing Premature Peer Disconnection - (File: `sync/src/types/mod.rs`)

### Summary
In `sync/src/types/mod.rs`, the `DownloadScheduler::decrease` function assigns `self.timeout_count` using `self.task_count` as the base instead of `self.timeout_count`. This mirrors the external report's pattern exactly: a value is set to `wrong_base + X` instead of being incremented by `X`. The result is that the two-strike grace period before penalizing a peer's `task_count` is completely bypassed — `task_count` is decremented on every slow block response instead of every third one, causing peers to be penalized 3× more aggressively than intended and eventually disconnected.

### Finding Description

`DownloadScheduler` tracks how many concurrent block download tasks a peer is allowed (`task_count`, initialized to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`, capped at `MAX_BLOCKS_IN_TRANSIT_PER_PEER = 128`). When a block response is slow, `decrease(num)` is called to accumulate a timeout counter and only penalize `task_count` after the counter exceeds 2.

The buggy code:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num);  // BUG
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

The correct code should be:

```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```

Because `task_count` starts at 32, `self.task_count.saturating_add(num)` always evaluates to at least 33 (when `num = 1`) or 34 (when `num = 2`). Since 33 > 2, the `if` branch fires on **every single call**, decrementing `task_count` by 1 and resetting `timeout_count` to 0. The grace period counter never accumulates — it is overwritten with a large value derived from the wrong variable on every invocation. [1](#0-0) 

`decrease` is called from `remove_by_block` when a block's download time falls in the `NormalToUpper` or `UpperToMax` time quantile: [2](#0-1) 

`task_count` reaching 0 triggers peer disconnection in `prune`: [3](#0-2) 

The constants involved: [4](#0-3) 

### Impact Explanation

The adaptive download scheduler is designed to give peers a grace period: only after accumulating more than 2 slow responses should `task_count` be decremented. With the bug, `task_count` is decremented on every slow response. Starting from 32, after 32 slow block responses the peer's `task_count` reaches 0 and the peer is disconnected. The intended behavior would require 3× as many slow responses (≈96) before reaching the same outcome. This means:

- Legitimate peers experiencing transient network latency are disconnected far sooner than the protocol intends.
- The sync state machine's adaptive bandwidth estimation is corrupted: `task_count` collapses to 0 much faster, starving the node of block download capacity from that peer.
- An adversarial sync peer that deliberately responds slowly (but not slowly enough to trigger the hard `BLOCK_DOWNLOAD_TIMEOUT` ban) can force the victim node to exhaust its `task_count` against that peer in 32 slow responses instead of ~96, causing premature disconnection and degrading sync throughput.

### Likelihood Explanation

The `decrease` path is exercised whenever a block response time falls in the upper two quantiles of the `TimeAnalyzer` distribution, which is a normal occurrence under any non-ideal network condition. The bug is triggered on every production node that has more than `protect_num` sync peers and has `adjustment = true` (the default). Any unprivileged sync peer that responds slowly — whether deliberately or due to load — will trigger this path.

### Recommendation

Change line 522 from:

```rust
self.timeout_count = self.task_count.saturating_add(num);
```

to:

```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```

This restores the intended grace-period semantics: `task_count` is only decremented after `timeout_count` accumulates beyond 2 slow responses.

### Proof of Concept

1. `DownloadScheduler` is initialized with `task_count = 32`, `timeout_count = 0`.
2. A sync peer responds slowly to a block request; `remove_by_block` calls `set.decrease(1)`.
3. Inside `decrease`: `self.timeout_count = 32.saturating_add(1) = 33`. Since `33 > 2`, `task_count` becomes `31`, `timeout_count` resets to `0`.
4. On the next slow response: `self.timeout_count = 31.saturating_add(1) = 32 > 2` → `task_count = 30`, `timeout_count = 0`.
5. After 32 such events, `task_count = 0`. In `prune`, the peer is inserted into `disconnect_list` and removed.
6. Under the correct implementation, `timeout_count` would need to accumulate past 2 before any decrement, requiring at least 3 slow responses per `task_count` decrement — meaning ~96 slow responses to reach `task_count = 0`.

### Citations

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

**File:** util/constant/src/sync.rs (L14-16)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
/// Maximum number of download blocks that can be requested at one time
pub const MAX_BLOCKS_IN_TRANSIT_PER_PEER: usize = 128;
```
