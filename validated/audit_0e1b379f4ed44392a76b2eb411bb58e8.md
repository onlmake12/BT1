### Title
`DownloadScheduler::decrease` Overwrites `timeout_count` With Wrong Field, Breaking Peer Penalty Accounting - (File: sync/src/types/mod.rs)

### Summary

The `DownloadScheduler::decrease` function in the sync subsystem contains a typo: it assigns `self.task_count.saturating_add(num)` to `self.timeout_count` instead of `self.timeout_count.saturating_add(num)`. This means `timeout_count` — the accumulator that is supposed to gate how aggressively `task_count` is reduced — is never accumulated from its previous value. The intended "accumulate slow-response events before penalizing" threshold is completely bypassed.

### Finding Description

`DownloadScheduler` controls how many blocks a node may request in parallel from a single peer. `task_count` is the concurrency budget; `timeout_count` is supposed to accumulate slow-response events and only trigger a decrement of `task_count` once it exceeds 2. [1](#0-0) 

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num);  // BUG: should be self.timeout_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

The correct line is `self.timeout_count = self.timeout_count.saturating_add(num)`. Because of the typo, `timeout_count` is always set to `task_count + num`, discarding whatever was previously accumulated. `task_count` starts at `INIT_BLOCKS_IN_TRANSIT_PER_PEER` (16) and is bounded above by `MAX_BLOCKS_IN_TRANSIT_PER_PEER`. [2](#0-1) 

This produces two distinct broken behaviors:

**Case A — `task_count > 2`:** `task_count + num` is always > 2, so `task_count` is decremented on *every single* slow-block response. The intended "accumulate 2 events before penalizing" gate is never enforced. The `timeout_count` field is effectively dead.

**Case B — `task_count = 1`, `num = 1`:** `1 + 1 = 2`, which is **not** `> 2`, so `task_count` is *never* decremented further via this path. A peer that has already been penalized to `task_count = 1` can continue delivering slow block responses indefinitely without triggering any additional penalty through `decrease`. The counter that should accumulate never does, so the threshold is never crossed.

`decrease` is called from `remove_by_block` when a received block's elapsed time falls in the `NormalToUpper` or `UpperToMax` quantile: [3](#0-2) 

### Impact Explanation

**Case A** causes legitimate peers with occasional slow responses (e.g., due to transient network congestion) to have their `task_count` reduced to 0 far faster than intended, triggering premature disconnection. This degrades the victim node's sync connectivity and, in aggregate, can reduce the diversity of its peer set, increasing susceptibility to eclipse-style isolation.

**Case B** allows a peer that has been penalized to `task_count = 1` to persist in that state indefinitely while continuing to deliver slow blocks. The node wastes a download slot on a consistently slow peer without ever evicting it via this code path. (Eviction via `prune`/`punish` for full timeouts still applies, but slow-but-received blocks are handled only by `decrease`.)

Both effects stem from the same root cause: `timeout_count` is never properly accumulated, so the resource-accounting counter that gates the penalty is always either trivially exceeded or trivially not exceeded.

### Likelihood Explanation

Any connected peer that sends block responses in the `NormalToUpper` or `UpperToMax` time quantile triggers `decrease`. This is a normal network condition. An adversarial peer can deliberately tune its response latency to stay just below the full-timeout threshold (`BLOCK_DOWNLOAD_TIMEOUT`) while remaining in the slow quantile, exploiting Case B to maintain a persistent low-penalty connection. No special privileges are required — only a standard P2P connection.

### Recommendation

Fix the typo on line 522:

```rust
// Before (buggy):
self.timeout_count = self.task_count.saturating_add(num);

// After (correct):
self.timeout_count = self.timeout_count.saturating_add(num);
```

This restores the intended behavior: `timeout_count` accumulates across calls, and `task_count` is only decremented after the accumulated count exceeds 2.

### Proof of Concept

With the current code, starting from `task_count = 16, timeout_count = 0`:

| Call | Buggy `timeout_count` | `task_count` after | Correct `timeout_count` | `task_count` after |
|---|---|---|---|---|
| `decrease(1)` | `16+1=17 > 2` → reset | **15** | `0+1=1 ≤ 2` | 16 |
| `decrease(1)` | `15+1=16 > 2` → reset | **14** | `1+1=2 ≤ 2` | 16 |
| `decrease(1)` | `14+1=15 > 2` → reset | **13** | `2+1=3 > 2` → reset | **15** |

With the bug, `task_count` drops by 1 on every call. With the correct code, it drops by 1 only after 3 accumulated slow responses. For Case B (`task_count=1, num=1`): buggy `timeout_count = 2`, condition `2 > 2` is false, `task_count` stays at 1 forever through this path — the counter that should accumulate never does, mirroring the "available count never decremented" pattern from the reference report. [4](#0-3)

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
