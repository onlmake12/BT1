### Title
`DownloadScheduler::decrease` Uses Wrong Accumulator Base, Causing Inflated Penalty and Premature Peer Disconnection — (`sync/src/types/mod.rs`)

---

### Summary

`DownloadScheduler::decrease` contains a state accounting error directly analogous to the Bribe.sol `totalVoting` bug: the `timeout_count` accumulator is never actually accumulated — it is overwritten with `task_count + num` on every call instead of `timeout_count + num`. Because `task_count` starts at 32 and is always > 2, the threshold check fires on every single slow-block event, causing `task_count` to be decremented far more aggressively than the protocol intends. This miscalibrates the block download scheduler for every sync peer and causes legitimate peers to be disconnected prematurely.

---

### Finding Description

In `sync/src/types/mod.rs`, the `DownloadScheduler` struct tracks how many concurrent block-download tasks a peer is allowed: [1](#0-0) 

The `decrease` function is intended to implement a **soft penalty**: accumulate timeout events in `timeout_count`, and only reduce `task_count` after at least 2 timeouts have been accumulated: [2](#0-1) 

**The bug is on line 522.** It reads:

```rust
self.timeout_count = self.task_count.saturating_add(num);
```

It should read:

```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```

Because `task_count` is initialized to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`: [3](#0-2) 

…`self.task_count.saturating_add(num)` evaluates to at least `32 + 1 = 33` on the very first call. Since `33 > 2` is always true, the body executes immediately: `task_count` is decremented and `timeout_count` is reset to 0. The accumulation mechanism is completely bypassed — `timeout_count` never actually accumulates anything.

`decrease` is called from `remove_by_block` whenever a received block's elapsed time falls in the `NormalToUpper` or `UpperToMax` quantile: [4](#0-3) 

The `prune` function then disconnects any peer whose `task_count` reaches 0: [5](#0-4) 

---

### Impact Explanation

The intended design is: tolerate up to 2 accumulated slow responses before penalizing `task_count`. The actual behavior is: penalize `task_count` on **every** slow response. Starting from `task_count = 32`, a peer that consistently responds in the `NormalToUpper` range will have its `task_count` reach 0 after only 32 slow responses instead of the intended 64+. At `task_count = 0`, the node disconnects from that peer.

Consequences:
1. **Inflated penalty rate**: `task_count` (the number of concurrent block requests allowed per peer) is decremented on every slow block, not every 2nd slow block. The scheduler is miscalibrated for all peers experiencing any network latency.
2. **Premature peer disconnection**: Legitimate peers with slightly elevated latency (between `normal_time` ~1250 ms and `low_time` ~1500 ms) are disconnected twice as fast as the protocol intends, degrading the node's sync connectivity.
3. **Sync slowdown**: Reduced `task_count` means fewer concurrent block requests per peer (`can_fetch()` returns a smaller value), directly slowing block synchronization throughput. [6](#0-5) 

---

### Likelihood Explanation

This is triggered by any sync peer whose block responses fall in the `NormalToUpper` or `UpperToMax` time quantile — a normal condition on any congested or geographically distant network link. No special privileges are required. Any unprivileged peer participating in the CKB sync protocol can trigger this path simply by responding to `GetBlocks` requests with blocks that arrive between 1250 ms and 1500 ms. The code path is exercised continuously during IBD (Initial Block Download) and normal sync.

---

### Recommendation

Fix line 522 to accumulate into `timeout_count` rather than overwrite it with `task_count`:

```rust
fn decrease(&mut self, num: usize) {
    // was: self.timeout_count = self.task_count.saturating_add(num);
    self.timeout_count = self.timeout_count.saturating_add(num);
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

This restores the intended soft-penalty semantics: `task_count` is only decremented after 2+ accumulated slow responses, not on every single one.

---

### Proof of Concept

Given `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`:

```
Initial state: task_count = 32, timeout_count = 0

Call decrease(1):
  timeout_count = task_count + 1 = 33   ← BUG: should be timeout_count + 1 = 1
  33 > 2 → true
  task_count = 31, timeout_count = 0    ← penalized immediately

Call decrease(1) again:
  timeout_count = task_count + 1 = 32   ← BUG: should be timeout_count + 1 = 1
  32 > 2 → true
  task_count = 30, timeout_count = 0    ← penalized again immediately

... after 32 calls to decrease(1):
  task_count = 0 → peer disconnected
```

With the correct fix:
```
Call decrease(1):
  timeout_count = 0 + 1 = 1
  1 > 2 → false → no penalty yet

Call decrease(1):
  timeout_count = 1 + 1 = 2
  2 > 2 → false → no penalty yet

Call decrease(1):
  timeout_count = 2 + 1 = 3
  3 > 2 → true → task_count decremented, timeout_count reset
```

The attacker-controlled entry path is: connect as a sync peer → respond to `GetBlocks` with blocks arriving in the 1250–1500 ms window → `remove_by_block` → `time_analyzer.push_time` returns `NormalToUpper` → `decrease(1)` → bug fires → `task_count` decremented immediately on every response. [2](#0-1)

### Citations

**File:** sync/src/types/mod.rs (L482-497)
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
```

**File:** sync/src/types/mod.rs (L504-506)
```rust
    fn can_fetch(&self) -> usize {
        self.task_count.saturating_sub(self.hashes.len())
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
