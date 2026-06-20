### Title
Wrong Variable Used in `DownloadScheduler::decrease` Causes Aggressive Peer Disconnection - (File: sync/src/types/mod.rs)

### Summary

`DownloadScheduler::decrease` uses `self.task_count` instead of `self.timeout_count` when accumulating timeout events. Because `task_count` starts at 32 (`INIT_BLOCKS_IN_TRANSIT_PER_PEER`) and is always greater than 2, the threshold check fires on **every single call**, decrementing `task_count` by 1 each time. The intended "accumulate 2+ timeouts before penalizing" logic never activates. When `task_count` reaches 0, the peer is disconnected.

### Finding Description

In `sync/src/types/mod.rs`, the `DownloadScheduler` struct tracks how many concurrent block-download tasks a peer is allowed (`task_count`) and how many consecutive timeouts have been observed (`timeout_count`). The `decrease` method is supposed to increment `timeout_count` and only reduce `task_count` after accumulating more than 2 timeouts: [1](#0-0) 

```rust
fn decrease(&mut self, num: usize) {
    // BUG: uses self.task_count instead of self.timeout_count
    self.timeout_count = self.task_count.saturating_add(num);
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

Because `task_count` is initialized to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`: [2](#0-1) 

...the expression `self.task_count.saturating_add(num)` always evaluates to at least `32 + 1 = 33`, which is always `> 2`. The threshold branch fires unconditionally on every call, decrementing `task_count` by 1 and resetting `timeout_count` to 0. The accumulated-timeout guard is completely bypassed.

This is the same class of bug as the VaderRouter report: a wrong variable of the same type is substituted for the intended one in a positional expression, silently producing incorrect semantics.

### Impact Explanation

When `task_count` reaches 0, the peer is evicted from `download_schedulers` and added to `disconnect_list`: [3](#0-2) 

A peer that should require 3+ timeout events before being penalized is instead penalized on every single call to `decrease`. This causes:
1. Legitimate sync peers to be disconnected far sooner than the protocol intends.
2. The node's block-download parallelism to collapse to 0 prematurely, stalling IBD (Initial Block Download) or normal sync.
3. Incorrect peer scoring: peers that behave correctly but trigger `decrease` for any reason are treated as if they have accumulated 3 timeouts.

### Likelihood Explanation

Any unprivileged sync peer that participates in block downloading can trigger the `decrease` path through normal protocol interaction. The sync state machine calls `decrease` as part of its scheduler adjustment logic during block download. No special privileges, keys, or majority hashpower are required — ordinary block relay activity is sufficient to trigger the bug on the receiving node.

### Recommendation

Change line 522 from:
```rust
self.timeout_count = self.task_count.saturating_add(num);
```
to:
```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```

This restores the intended accumulation semantics: `task_count` is only reduced after `timeout_count` exceeds 2 consecutive timeout events.

### Proof of Concept

Given `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32` and `MAX_BLOCKS_IN_TRANSIT_PER_PEER = 128`:

1. A `DownloadScheduler` is created with `task_count = 32`, `timeout_count = 0`.
2. `decrease(1)` is called (e.g., on a slow-block event).
3. `self.timeout_count = self.task_count.saturating_add(1) = 32 + 1 = 33`.
4. `33 > 2` → `task_count` becomes `31`, `timeout_count` reset to `0`.
5. On the next call: `self.timeout_count = 31 + 1 = 32 > 2` → `task_count` becomes `30`.
6. After 32 calls, `task_count = 0` → peer is disconnected.

The intended behavior requires `timeout_count` to accumulate across calls and only cross the threshold of 2 after multiple events. With the bug, the threshold is crossed on the very first call and every subsequent call, making the guard meaningless. [4](#0-3)

### Citations

**File:** sync/src/types/mod.rs (L482-532)
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

    fn punish(&mut self, exp: usize) {
        self.task_count >>= exp
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

**File:** util/constant/src/sync.rs (L14-16)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
/// Maximum number of download blocks that can be requested at one time
pub const MAX_BLOCKS_IN_TRANSIT_PER_PEER: usize = 128;
```
