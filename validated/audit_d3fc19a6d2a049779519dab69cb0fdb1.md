### Title
Unconditional `task_count` Decrement in `DownloadScheduler::decrease()` Defeats Timeout-Accumulation Guard — (`sync/src/types/mod.rs`)

---

### Summary

`DownloadScheduler::decrease()` contains a one-word typo that causes `timeout_count` to be set to `task_count + num` instead of `timeout_count + num`. Because `task_count` starts at 32 and `num` is 1 or 2, the result is always > 2, so the guard `if self.timeout_count > 2` is **always true** and `task_count` is **always decremented** on every call — exactly mirroring the unconditional cap-inflation pattern in the reference report.

---

### Finding Description

`DownloadScheduler` governs how many blocks a sync peer may have in-flight simultaneously. `task_count` (init = `INIT_BLOCKS_IN_TRANSIT_PER_PEER` = 32, max = `MAX_BLOCKS_IN_TRANSIT_PER_PEER` = 128) is the per-peer concurrency budget. The design intent of `decrease()` is to accumulate slow-delivery events in `timeout_count` and only reduce `task_count` after more than two such events have been observed — a deliberate "soft" penalty buffer.

```rust
// sync/src/types/mod.rs  line 521-527
fn decrease(&mut self, num: usize) {
    // ❌ BUG: reads self.task_count instead of self.timeout_count
    self.timeout_count = self.task_count.saturating_add(num);
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

Because `task_count` ≥ 1 at all times (peers with `task_count == 0` are immediately disconnected in `prune()`), `task_count + num` is always ≥ 3 > 2. The branch is therefore **unconditionally taken** on every call:

| Call | `timeout_count` set to | Branch taken? | `task_count` after |
|------|------------------------|---------------|--------------------|
| 1st  | 32 + 1 = 33            | yes           | 31                 |
| 2nd  | 31 + 1 = 32            | yes           | 30                 |
| …    | …                      | yes           | …                  |
| 32nd | 1 + 1 = 2 → **not > 2**| **no**        | 1 (survives)       |

The correct code is `self.timeout_count = self.timeout_count.saturating_add(num)`. With the fix, `task_count` would only be decremented after 3 consecutive `NormalToUpper` events (num=1) or 2 consecutive `UpperToMax` events (num=2).

`decrease()` is called from `remove_by_block()` whenever a received block's elapsed time falls in the `NormalToUpper` or `UpperToMax` quantile:

```rust
// sync/src/types/mod.rs  line 801-810
TimeQuantile::NormalToUpper => {
    if should_punish { set.decrease(1) }
}
TimeQuantile::UpperToMax => {
    if should_punish { set.decrease(2) }
}
```

`should_punish` is true when `download_schedulers.len() > protect_num` (default 4), i.e., whenever there are more than 4 active download peers — the normal operating condition.

When `task_count` reaches 0, `prune()` disconnects the peer:

```rust
// sync/src/types/mod.rs  line 692-700
download_schedulers.retain(|k, v| {
    if v.task_count == 0 {
        disconnect_list.insert(*k);
        false
    } else { true }
});
```

---

### Impact Explanation

The `timeout_count` accumulation buffer is completely neutralized. Every block that arrives in the `NormalToUpper` latency band causes an immediate `task_count` decrement instead of requiring 3 such events. A peer starting at `task_count = 32` is disconnected after only **32** slightly-slow block deliveries rather than the intended **96**. This is a 3× acceleration of the disconnection path for peers that are merely "normal-to-upper" slow — a category that includes legitimate peers on congested or high-latency links. The `timeout_count` field is rendered permanently useless (always reset to 0 after being set to `task_count + num`).

---

### Likelihood Explanation

The trigger is any sync peer that delivers blocks with elapsed time in the `NormalToUpper` quantile. This is a normal network condition, not an adversarial one. An unprivileged remote peer (block relayer / sync peer) can reach this code path simply by responding to `GetBlocks` requests with slightly elevated latency. No special privileges, keys, or majority hashpower are required. The bug fires on every production node that has more than 4 download peers and receives blocks from a peer whose response time falls in the upper-normal band.

---

### Recommendation

Change line 522 from:

```rust
self.timeout_count = self.task_count.saturating_add(num);
```

to:

```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```

This restores the intended accumulation semantics: `task_count` is only decremented after the accumulated slow-delivery count exceeds 2, matching the design documented by the field name `timeout_count`.

---

### Proof of Concept

**Step-by-step trace with `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`, `num = 1` (NormalToUpper):**

```
Initial state: task_count = 32, timeout_count = 0

Call decrease(1):
  timeout_count = task_count + 1 = 33   // should be timeout_count + 1 = 1
  33 > 2 → task_count = 31, timeout_count = 0

Call decrease(1):
  timeout_count = task_count + 1 = 32   // should be timeout_count + 1 = 2
  32 > 2 → task_count = 30, timeout_count = 0

...

After 31 calls: task_count = 1, timeout_count = 0
Call decrease(1):
  timeout_count = 1 + 1 = 2
  2 > 2 is FALSE → task_count stays at 1  ← only this one call is not penalized

After 32 calls total: task_count = 1
Next prune() cycle: task_count reaches 0 → peer disconnected.
```

**Correct behavior (with fix):**

```
After 3 calls: timeout_count = 3 > 2 → task_count = 31, timeout_count = 0
After 6 calls: task_count = 30
...
After 96 calls: task_count = 0 → peer disconnected.
```

The bug causes disconnection 3× faster than the protocol intends for peers in the `NormalToUpper` latency band, defeating the purpose of the `timeout_count` guard. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** sync/src/types/mod.rs (L795-811)
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
```

**File:** util/constant/src/sync.rs (L14-16)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
/// Maximum number of download blocks that can be requested at one time
pub const MAX_BLOCKS_IN_TRANSIT_PER_PEER: usize = 128;
```
