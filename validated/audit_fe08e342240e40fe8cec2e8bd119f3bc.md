### Title
`DownloadScheduler::decrease()` Uses Wrong Variable for `timeout_count` Accumulation, Causing Aggressive `task_count` Depletion and Premature Peer Disconnection — (`File: sync/src/types/mod.rs`)

---

### Summary

In `sync/src/types/mod.rs`, the `DownloadScheduler::decrease()` function contains a counter accounting bug: `timeout_count` is assigned `self.task_count.saturating_add(num)` instead of `self.timeout_count.saturating_add(num)`. Because `task_count` starts at 32 (`INIT_BLOCKS_IN_TRANSIT_PER_PEER`), the condition `timeout_count > 2` is always true on every call, causing `task_count` to be decremented on every slow block response rather than only after accumulating 2 slow responses as intended. When `task_count` reaches 0, the peer is disconnected.

---

### Finding Description

`DownloadScheduler` tracks how many blocks a node may request from a given sync peer simultaneously. The `task_count` field is the concurrency budget; `timeout_count` is meant to accumulate slow-response events and only trigger a penalty after 2 such events.

The buggy function:

```rust
// sync/src/types/mod.rs, line 521-527
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num); // BUG: should be self.timeout_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

The correct line should be:
```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```

Because `task_count` initializes to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`, the expression `self.task_count.saturating_add(num)` always evaluates to ≥ 33, which is always `> 2`. Therefore:
- `task_count` is decremented by 1 on **every** call to `decrease()`
- `timeout_count` is immediately reset to 0 after each call
- The intended "accumulate 2 slow responses before penalizing" guard is completely bypassed

`decrease()` is called from `remove_by_block()` when a received block's elapsed time falls in the `NormalToUpper` or `UpperToMax` quantile:

```rust
// sync/src/types/mod.rs, line 800-810
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

When `task_count` reaches 0, `prune()` disconnects the peer:

```rust
// sync/src/types/mod.rs, line 692-700
download_schedulers.retain(|k, v| {
    if v.task_count == 0 {
        disconnect_list.insert(*k);
        false
    } else {
        true
    }
});
```

---

### Impact Explanation

- **Block sync degradation**: A node's block download concurrency budget (`task_count`) for any peer is depleted 2× faster than intended (every slow response instead of every 2nd slow response). Under variable-latency network conditions, `task_count` can reach 0 quickly, causing the node to disconnect from legitimate sync peers.
- **Premature peer disconnection**: Any sync peer whose block responses fall in the `NormalToUpper` or `UpperToMax` time quantile will cause the local node to disconnect from it after only 32 slow responses (instead of 64), degrading the node's ability to sync the chain.
- **Attacker-amplified**: A malicious sync peer can deliberately send blocks just slowly enough to stay in the `NormalToUpper` quantile, causing the victim node to exhaust its `task_count` and disconnect from that peer (or other peers if the scheduler is shared), disrupting IBD or normal sync.

---

### Likelihood Explanation

The `decrease()` path is triggered whenever a block arrives in the `NormalToUpper` or `UpperToMax` time quantile and `should_punish` is true (i.e., more than `protect_num = 4` download peers are connected). Under real-world network conditions with variable latency, this is a common occurrence. The bug is always active — no special configuration is required. Any unprivileged sync peer that sends blocks with slightly elevated latency will trigger it.

---

### Recommendation

Fix line 522 in `sync/src/types/mod.rs` to accumulate into `self.timeout_count` rather than overwrite it with `self.task_count`:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.timeout_count.saturating_add(num); // fixed
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

---

### Proof of Concept

**Initial state**: `task_count = 32`, `timeout_count = 0`

**Step 1**: Peer sends a block that arrives in `NormalToUpper` quantile → `decrease(1)` called.
- Buggy: `timeout_count = 32 + 1 = 33 > 2` → `task_count = 31`, `timeout_count = 0`
- Correct: `timeout_count = 0 + 1 = 1 ≤ 2` → no penalty

**Step 2**: Same peer sends another slow block → `decrease(1)` called again.
- Buggy: `timeout_count = 31 + 1 = 32 > 2` → `task_count = 30`, `timeout_count = 0`
- Correct: `timeout_count = 1 + 1 = 2 ≤ 2` → still no penalty

After 32 slow responses:
- Buggy: `task_count = 0` → peer disconnected by `prune()`
- Correct: `task_count = 31` (only penalized after every 2 slow responses)

The root cause is confirmed at: [1](#0-0) 

The `task_count` initial value and the disconnect condition: [2](#0-1) [3](#0-2) 

The call sites in `remove_by_block()`: [4](#0-3) 

The constant `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`: [5](#0-4)

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

**File:** sync/src/types/mod.rs (L800-811)
```rust
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
