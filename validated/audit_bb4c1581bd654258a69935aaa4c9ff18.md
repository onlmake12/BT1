### Title
`DownloadScheduler::decrease` Uses Wrong Variable in State Update, Causing Overly Aggressive Peer Disconnection — (`sync/src/types/mod.rs`)

### Summary

`DownloadScheduler::decrease` contains a state update ordering bug where `self.task_count` is used instead of `self.timeout_count` when accumulating the slow-response counter. This completely bypasses the intended "accumulate 3 slow responses before penalizing" logic, causing `task_count` to be decremented on every single slow block response. Since `task_count == 0` triggers peer disconnection in `prune()`, peers are disconnected far more aggressively than the protocol intends.

### Finding Description

In `sync/src/types/mod.rs`, the `DownloadScheduler` struct tracks per-peer block download capacity:

```rust
pub struct DownloadScheduler {
    task_count: usize,    // how many blocks this peer can be asked for concurrently
    timeout_count: usize, // accumulator: how many slow responses before penalizing
    hashes: HashSet<BlockNumberAndHash>,
}
```

The `decrease` function is called from `remove_by_block` when a received block's elapsed time falls in the `NormalToUpper` or `UpperToMax` quantile (i.e., the peer was slow but not timed out): [1](#0-0) 

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num); // BUG: should be self.timeout_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

Line 522 reads `self.task_count.saturating_add(num)` instead of `self.timeout_count.saturating_add(num)`. The intended design is to accumulate `timeout_count` across multiple calls and only reduce `task_count` after 3 or more accumulated slow responses. Instead:

- `timeout_count` is set to `task_count + num` on every call (never accumulated from its previous value)
- Since `task_count` starts at `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32` and `num` is 1 or 2, the result is always ≥ 33, which is always `> 2`
- Therefore `task_count` is decremented and `timeout_count` is reset to 0 on **every** call to `decrease` [2](#0-1) 

The `prune()` function disconnects any peer whose `task_count` reaches 0: [3](#0-2) 

`decrease` is called from `remove_by_block` for slow-arriving blocks: [4](#0-3) 

### Impact Explanation

Starting from `task_count = 32`, a peer needs only **32 slow block responses** to reach `task_count = 0` and be disconnected. The intended behavior (accumulate 3 slow responses per decrement) would require **96 slow responses**. The penalty is 3× more aggressive than designed.

This means:
1. Legitimate peers with slightly elevated latency (between `normal_time` ~1250ms and `low_time` ~1500ms) are disconnected far faster than intended, degrading network connectivity.
2. A malicious peer that deliberately responds slowly (but within the non-timeout window) can exploit this to cause the victim node to exhaust its `task_count` faster, triggering disconnection and reducing the victim's peer pool.
3. During network congestion when many peers are slow, a node may disconnect from all of them simultaneously, leaving it isolated.

### Likelihood Explanation

Any unprivileged P2P peer can trigger this by responding to `GetBlocks` requests with a delay in the `NormalToUpper` or `UpperToMax` time range. No special privileges, keys, or majority hashpower are required. The entry path is the standard block download protocol reachable by any connected peer.

### Recommendation

Fix line 522 to accumulate `timeout_count` from its previous value:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.timeout_count.saturating_add(num); // was: self.task_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

This restores the intended behavior: only reduce `task_count` after accumulating more than 2 slow-response events.

### Proof of Concept

With `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32` and `num = 1`:

**Buggy behavior** (current):
- Call 1: `timeout_count = 32 + 1 = 33 > 2` → `task_count = 31`, `timeout_count = 0`
- Call 2: `timeout_count = 31 + 1 = 32 > 2` → `task_count = 30`, `timeout_count = 0`
- … after 32 slow blocks: `task_count = 0` → peer disconnected

**Intended behavior** (fixed):
- Call 1: `timeout_count = 0 + 1 = 1`, not > 2, no penalty
- Call 2: `timeout_count = 1 + 1 = 2`, not > 2, no penalty
- Call 3: `timeout_count = 2 + 1 = 3 > 2` → `task_count = 31`, `timeout_count = 0`
- … after 96 slow blocks: `task_count = 0` → peer disconnected [5](#0-4)

### Citations

**File:** sync/src/types/mod.rs (L482-527)
```rust
#[derive(Debug, Clone)]
pub struct DownloadScheduler {
    task_count: usize,
    timeout_count: usize,
    hashes: HashSet<BlockNumberAndHash>,
}

impl Default for DownloadScheduler {
    fn default() -> Self {
        Self {
            hashes: HashSet::default(),
            task_count: INIT_BLOCKS_IN_TRANSIT_PER_PEER,
            timeout_count: 0,
        }
    }
}

impl DownloadScheduler {
    fn inflight_count(&self) -> usize {
        self.hashes.len()
    }

    fn can_fetch(&self) -> usize {
        self.task_count.saturating_sub(self.hashes.len())
    }

    pub(crate) const fn task_count(&self) -> usize {
        self.task_count
    }

    fn increase(&mut self, num: usize) {
        if self.task_count < MAX_BLOCKS_IN_TRANSIT_PER_PEER {
            self.task_count = ::std::cmp::min(
                self.task_count.saturating_add(num),
                MAX_BLOCKS_IN_TRANSIT_PER_PEER,
            )
        }
    }

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

**File:** sync/src/types/mod.rs (L797-811)
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
```

**File:** util/constant/src/sync.rs (L14-16)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
/// Maximum number of download blocks that can be requested at one time
pub const MAX_BLOCKS_IN_TRANSIT_PER_PEER: usize = 128;
```
