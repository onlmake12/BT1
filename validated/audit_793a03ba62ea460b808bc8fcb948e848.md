### Title
Incorrect Tracking Variable in `DownloadScheduler::decrease` Causes Bypassed Dampening and Premature Peer Penalization - (File: sync/src/types/mod.rs)

### Summary
`DownloadScheduler::decrease` uses `self.task_count` instead of `self.timeout_count` as the base when accumulating the timeout counter. Because `task_count` always starts well above the threshold of 2, the dampening guard (`timeout_count > 2`) fires on every single call, causing `task_count` to be decremented on every slow-block event rather than only after three accumulated slow events as the design intends.

### Finding Description
`DownloadScheduler` tracks how many concurrent block-download slots a peer is allowed (`task_count`) and uses `timeout_count` as a dampening accumulator so that `task_count` is only reduced after three consecutive slow-download events. [1](#0-0) 

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num);  // ← BUG
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

The assignment on line 522 reads `self.task_count.saturating_add(num)` instead of `self.timeout_count.saturating_add(num)`. Because `task_count` is initialised to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` (32 in tests) and `num` is 1 or 2, the expression always evaluates to ≥ 33, which is always `> 2`. The `if`-branch therefore fires unconditionally, decrementing `task_count` and resetting `timeout_count` to 0 on every call. The accumulator never actually accumulates anything. [2](#0-1) 

The correct code should be:
```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```

`decrease` is called from `remove_by_block` whenever a received block's download latency falls in the `NormalToUpper` (decrease by 1) or `UpperToMax` (decrease by 2) quantile, subject to `should_punish`: [3](#0-2) 

`should_punish` is true whenever the number of connected download peers exceeds `protect_num`: [4](#0-3) 

### Impact Explanation
With the bug, every single slow block download immediately decrements `task_count` for the responsible peer. Without the bug, three slow events are required before any decrement. The practical consequences are:

1. **Premature `task_count` exhaustion.** `task_count` reaches 0 three times faster than intended. A peer whose `task_count` reaches 0 is disconnected in `prune`: [5](#0-4) 

2. **Reduced sync parallelism.** Legitimate peers experiencing transient latency spikes lose download slots immediately, degrading IBD and steady-state sync throughput.

3. **Incorrect scheduler state.** The `timeout_count` field is always reset to 0 after each call, so the intended three-strike dampening is permanently disabled for all peers.

### Likelihood Explanation
The code path is reachable by any unprivileged connected peer. During normal block synchronisation, `remove_by_block` is called for every downloaded block. On a loaded network or a slow machine, many blocks will fall in the `NormalToUpper`/`UpperToMax` latency buckets, triggering `decrease` repeatedly. No special privileges, keys, or majority hash-power are required.

### Recommendation
Change line 522 from:
```rust
self.timeout_count = self.task_count.saturating_add(num);
```
to:
```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```
This restores the intended three-strike dampening so that `task_count` is only decremented after three accumulated slow-download events, matching the design documented in the surrounding comments.

### Proof of Concept
Assume `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32` and `num = 1` (NormalToUpper penalty):

**With the bug:**
- Call 1: `timeout_count = 32 + 1 = 33 > 2` → `task_count = 31`, `timeout_count = 0`
- Call 2: `timeout_count = 31 + 1 = 32 > 2` → `task_count = 30`, `timeout_count = 0`
- Call 3: `timeout_count = 30 + 1 = 31 > 2` → `task_count = 29`, `timeout_count = 0`
- … `task_count` reaches 0 after 32 slow blocks.

**Without the bug (correct behaviour):**
- Call 1: `timeout_count = 0 + 1 = 1 ≤ 2` → no change to `task_count`
- Call 2: `timeout_count = 1 + 1 = 2 ≤ 2` → no change to `task_count`
- Call 3: `timeout_count = 2 + 1 = 3 > 2` → `task_count = 31`, `timeout_count = 0`
- … `task_count` reaches 0 after 96 slow blocks (3× slower penalisation).

The existing test at line 117 of `sync/src/tests/inflight_blocks.rs` observes `peer_can_fetch_count(2.into()) == 32 >> 4 = 2`, consistent with the accelerated penalisation produced by the bug. [6](#0-5)

### Citations

**File:** sync/src/types/mod.rs (L489-497)
```rust
impl Default for DownloadScheduler {
    fn default() -> Self {
        Self {
            hashes: HashSet::default(),
            task_count: INIT_BLOCKS_IN_TRANSIT_PER_PEER,
            timeout_count: 0,
        }
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

**File:** sync/src/types/mod.rs (L649-650)
```rust
        let should_punish = self.download_schedulers.len() > self.protect_num;
        let adjustment = self.adjustment;
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

**File:** sync/src/types/mod.rs (L797-812)
```rust
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

**File:** sync/src/tests/inflight_blocks.rs (L117-117)
```rust
    assert_eq!(inflight_blocks.peer_can_fetch_count(2.into()), 32 >> 4);
```
