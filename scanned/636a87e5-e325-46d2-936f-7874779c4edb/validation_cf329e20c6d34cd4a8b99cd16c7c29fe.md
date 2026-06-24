Audit Report

## Title
`DownloadScheduler::decrease()` reads `task_count` instead of `timeout_count`, bypassing slow-response accumulation guard - (File: sync/src/types/mod.rs)

## Summary
In `sync/src/types/mod.rs`, the `decrease()` method on `DownloadScheduler` incorrectly reads `self.task_count` instead of `self.timeout_count` when updating the accumulator. Because `task_count` is initialized to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` (always >> 2), the guard condition `timeout_count > 2` is satisfied on every call, causing `task_count` to be decremented on every slow block response rather than only after 3 or more accumulated slow responses. This causes peers to be disconnected approximately 3× faster than the protocol intends.

## Finding Description
The `DownloadScheduler` struct tracks per-peer block download concurrency:

```rust
// sync/src/types/mod.rs L482-497
pub struct DownloadScheduler {
    task_count: usize,      // initialized to INIT_BLOCKS_IN_TRANSIT_PER_PEER
    timeout_count: usize,   // initialized to 0
    hashes: HashSet<BlockNumberAndHash>,
}
```

The `decrease()` method at L521-527 contains the bug:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num);  // BUG: should be self.timeout_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

Since `task_count` starts at `INIT_BLOCKS_IN_TRANSIT_PER_PEER` and `num` is 1 or 2, the expression `task_count.saturating_add(num)` is always ≥ 3, so `timeout_count > 2` is always true. The accumulation guard is completely bypassed — `task_count` is decremented and `timeout_count` is reset to 0 on every single call.

`decrease()` is called from `remove_by_block()` at L797-811 on every completed block download that falls in the `NormalToUpper` or `UpperToMax` time quantile. When `task_count` reaches 0, `prune()` at L692-700 adds the peer to `disconnect_list` and removes it.

## Impact Explanation
This matches **Low (501–2000 points): Any other important performance improvements for CKB**. The bug degrades sync performance by disconnecting peers with variable latency ~3× faster than the protocol intends (after 32 slow responses instead of ~96). This reduces block download parallelism and degrades sync throughput for any node experiencing normal network jitter. The impact does not rise to High (no node crash, no network-wide congestion) because the effect is bounded to per-peer concurrency reduction and premature disconnection of individual peers.

## Likelihood Explanation
The `remove_by_block()` path is exercised on every completed block download. Any peer whose responses fall in the `NormalToUpper` or `UpperToMax` time quantile — a normal occurrence under variable network conditions — will trigger `decrease()`. No special attacker capability is required; the bug is always active for any syncing peer experiencing latency jitter.

## Recommendation
Change line 522 to accumulate into `self.timeout_count` rather than overwrite it with `self.task_count`:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.timeout_count.saturating_add(num); // fix: was self.task_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

## Proof of Concept
1. A sync peer connects; its `DownloadScheduler` is initialized with `task_count = INIT_BLOCKS_IN_TRANSIT_PER_PEER`, `timeout_count = 0`.
2. The peer's block responses consistently fall in the `NormalToUpper` quantile (normal under network jitter).
3. Each completed block download calls `remove_by_block()` → `decrease(1)`.
4. Inside `decrease(1)`: `self.timeout_count = self.task_count.saturating_add(1)`. With `task_count = 32`, `timeout_count` becomes 33. Since 33 > 2, `task_count` is decremented to 31 and `timeout_count` reset to 0.
5. On the next slow response: `timeout_count = 31 + 1 = 32 > 2` → `task_count` decremented to 30.
6. After 32 such responses, `task_count = 0`. The next `prune()` call disconnects the peer.
7. With the fix, `timeout_count` would accumulate: 0→1→2→3, only then decrement `task_count` once and reset, requiring ~96 slow responses before disconnection.

A unit test can confirm this by constructing a `DownloadScheduler`, calling `decrease(1)` 32 times, and asserting `task_count == 0` (demonstrating the bug) versus the fixed behavior where `task_count` remains at 31 after 32 calls. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
