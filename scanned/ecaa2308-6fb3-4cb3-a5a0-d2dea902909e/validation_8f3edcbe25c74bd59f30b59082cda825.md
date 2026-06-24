Audit Report

## Title
Broken `timeout_count` Accumulation in `DownloadScheduler::decrease` Causes Unconditional `task_count` Decrement on Every Slow Response — (`File: sync/src/types/mod.rs`)

## Summary
`DownloadScheduler::decrease` at line 522 assigns `self.timeout_count = self.task_count.saturating_add(num)` instead of `self.timeout_count.saturating_add(num)`. Because `task_count` initialises to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`, the expression always evaluates to ≥ 33, which is always `> 2`, so `task_count` is decremented on every single slow block response rather than only after accumulating 3+ slow responses. A peer reaches `task_count == 0` after 32 slow responses and is disconnected, making the penalty at least 3× more aggressive than the protocol intends.

## Finding Description
In `sync/src/types/mod.rs` at line 521–527:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num); // uses task_count, not timeout_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
``` [1](#0-0) 

`task_count` starts at `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32` (`util/constant/src/sync.rs` line 14). With `num` ∈ {1, 2}, `32 + 1 = 33 > 2` is always true, so the guard never acts as a threshold — `task_count` is decremented and `timeout_count` is reset to 0 on every call. The accumulator field `timeout_count` is never actually accumulated. [2](#0-1) 

`decrease` is called from `remove_by_block` when `adjustment` is true and `should_punish` is true (i.e., `download_schedulers.len() > protect_num`, default 4): [3](#0-2) 

When `task_count` reaches 0, `prune()` inserts the peer into `disconnect_list`: [4](#0-3) 

`should_punish` is evaluated at line 786 as `self.download_schedulers.len() > self.protect_num`, which is satisfied on any node with more than 4 download peers — a normal mainnet condition. [5](#0-4) 

## Impact Explanation
The intended design gives each peer a grace window: `task_count` is only decremented after 3+ accumulated slow responses. The bug removes this window entirely. Starting from 32, a peer is disconnected after exactly 32 slow responses instead of the intended ~96+. Any legitimately slow peer (high-latency link, loaded CPU) is disconnected 3× faster than the protocol intends, degrading the victim node's peer set quality and sync throughput. This fits the allowed impact: **Low (501–2000 points) — important performance/correctness degradation for a CKB node's sync mechanism**, with a secondary path toward sync stalls on nodes with limited peer counts.

## Likelihood Explanation
- Any unprivileged sync peer can trigger this by responding to `GetBlocks` with valid blocks in the `NormalToUpper` or `UpperToMax` time quantile (above `normal_time`, below `BLOCK_DOWNLOAD_TIMEOUT = 30 s`).
- `should_punish` is true on any node with more than 4 download peers — standard mainnet conditions.
- `adjustment` defaults to `true`, keeping the path active.
- No special keys, operator access, or majority hashpower required.
- The condition fires on every qualifying slow response, making it trivially repeatable.

## Recommendation
Change line 522 in `sync/src/types/mod.rs` from:

```rust
self.timeout_count = self.task_count.saturating_add(num);
```

to:

```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```

This restores the intended accumulator semantics: `task_count` is only decremented after the accumulated slow-response count exceeds 2, giving peers the designed grace window before penalisation.

## Proof of Concept
Unit test plan:
1. Construct a `DownloadScheduler` with default `task_count = INIT_BLOCKS_IN_TRANSIT_PER_PEER` (32) and `timeout_count = 0`.
2. Call `decrease(1)` once.
3. Assert `task_count == 31` (demonstrates unconditional decrement — bug present).
4. Assert `timeout_count == 0` (demonstrates accumulator is never used).
5. With the fix applied, assert `task_count == 32` (no decrement on first call) and `timeout_count == 1` (accumulator incremented).
6. Call `decrease(1)` two more times; assert `task_count` decrements to 31 only on the third call (after `timeout_count` exceeds 2).

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

**File:** sync/src/types/mod.rs (L786-786)
```rust
        let should_punish = self.download_schedulers.len() > self.protect_num;
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

**File:** util/constant/src/sync.rs (L14-14)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
```
