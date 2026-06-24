Audit Report

## Title
`DownloadScheduler::decrease` Overwrites `timeout_count` with `task_count`, Bypassing Soft-Penalty Accumulation — (`sync/src/types/mod.rs`)

## Summary
In `DownloadScheduler::decrease`, line 522 reads `self.timeout_count = self.task_count.saturating_add(num)` instead of `self.timeout_count = self.timeout_count.saturating_add(num)`. Because `task_count` is initialized to 32, the expression always evaluates to ≥ 33, which is always `> 2`, so the penalty branch fires on every single call. The intended two-strike accumulation buffer is completely bypassed, causing `task_count` to be decremented on every slow block response and peers to be disconnected after 32 slow responses instead of the intended 64+.

## Finding Description
`DownloadScheduler` is initialized with `task_count = INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32` and `timeout_count = 0` (`sync/src/types/mod.rs`, lines 489–497). The `decrease` function at line 521–527 is supposed to accumulate slow-response events in `timeout_count` and only decrement `task_count` after more than 2 have accumulated. However, line 522 writes `self.timeout_count = self.task_count.saturating_add(num)`, not `self.timeout_count.saturating_add(num)`. Since `task_count` starts at 32 and `num` is at least 1, the result is always ≥ 33, which always satisfies `> 2`. The accumulation guard is never effective: `task_count` is decremented and `timeout_count` is reset to 0 on every invocation. `decrease` is called from `remove_by_block` (lines 801–810) whenever `time_analyzer.push_time` returns `NormalToUpper` (calls `decrease(1)`) or `UpperToMax` (calls `decrease(2)`). Once `task_count` reaches 0, the `retain` closure at lines 692–700 inserts the peer into `disconnect_list` and removes it from `download_schedulers`.

## Impact Explanation
This is a sync performance degradation: `task_count` controls how many concurrent block requests are issued to a peer via `can_fetch()` (line 504–506). With the bug, any peer whose responses fall in the `NormalToUpper` or `UpperToMax` latency band has its `task_count` decremented on every response, reaching 0 after 32 slow responses and triggering disconnection. The intended design tolerates 2 accumulated slow responses before penalizing. This miscalibrates the block download scheduler for all peers experiencing normal network latency, reducing sync throughput and causing premature disconnection of legitimate peers. This matches the **Low (501–2000 points)** impact class: an important performance improvement for CKB sync.

## Likelihood Explanation
No special privileges are required. Any sync peer whose block responses arrive in the `NormalToUpper` or `UpperToMax` time quantile (normal on congested or geographically distant links) triggers this path. The condition is exercised continuously during IBD and normal sync. An adversarial peer can deliberately respond just slowly enough to stay in the penalized quantile, accelerating disconnection of itself or causing the local node to cycle through peers rapidly.

## Recommendation
Fix line 522 to accumulate into `timeout_count` rather than overwrite it with `task_count`:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.timeout_count.saturating_add(num); // was: self.task_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

## Proof of Concept
Unit test against the current implementation:

```rust
#[test]
fn test_decrease_accumulates_correctly() {
    let mut sched = DownloadScheduler::default(); // task_count=32, timeout_count=0
    sched.decrease(1);
    // BUG: timeout_count = 32+1 = 33 > 2 → task_count becomes 31 immediately
    assert_eq!(sched.task_count(), 31); // fires on first call, not after 3rd
    sched.decrease(1);
    assert_eq!(sched.task_count(), 30); // fires again immediately
    // After 32 calls: task_count = 0 → peer disconnected
}
```

With the fix applied, `task_count` should remain 32 after the first two calls and only decrement on the third.