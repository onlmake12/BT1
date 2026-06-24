Audit Report

## Title
`DownloadScheduler::decrease` Uses `self.task_count` Instead of `self.timeout_count` as Accumulator Base, Bypassing Grace Period and Causing Premature Peer Disconnection - (File: `sync/src/types/mod.rs`)

## Summary
In `sync/src/types/mod.rs`, `DownloadScheduler::decrease` at line 522 writes `self.timeout_count = self.task_count.saturating_add(num)` instead of `self.timeout_count = self.timeout_count.saturating_add(num)`. Because `task_count` is initialized to 32, the result always exceeds the threshold of 2, causing `task_count` to be decremented and `timeout_count` reset on every single slow-response event. The intended two-strike grace period is completely bypassed, causing peers to be penalized 3× more aggressively and disconnected after ~32 slow responses instead of ~96.

## Finding Description
`DownloadScheduler` is initialized with `task_count = INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32` and `timeout_count = 0` (lines 493–494). The `decrease` function is intended to accumulate `timeout_count` and only decrement `task_count` once the counter exceeds 2:

```rust
// sync/src/types/mod.rs L521-527
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num);  // BUG: uses task_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

Since `task_count` starts at 32, `self.task_count.saturating_add(1)` = 33 on the first call. `33 > 2` is always true, so `task_count` is decremented and `timeout_count` reset to 0 on every invocation. The accumulator never builds up.

`decrease` is called from `remove_by_block` (lines 801–810) whenever a block response time falls in the `NormalToUpper` (num=1) or `UpperToMax` (num=2) quantile, which is a routine occurrence under any non-ideal network condition. The `adjustment` flag defaults to `true`.

When `task_count` reaches 0, `prune` (lines 692–700) inserts the peer into `disconnect_list` and removes it from `download_schedulers`.

## Impact Explanation
This is a **Low (501–2000 points)** finding: an important performance regression for CKB sync. Legitimate peers experiencing transient latency are disconnected after ~32 slow block responses instead of the intended ~96. This collapses `task_count` to 0 three times faster than designed, starving the node of block download capacity from that peer and degrading sync throughput. An adversarial sync peer that deliberately responds slowly (but not slowly enough to trigger the hard `BLOCK_DOWNLOAD_TIMEOUT` ban) can exploit this to force premature disconnection with one-third the effort the protocol intends to require.

## Likelihood Explanation
The `NormalToUpper` and `UpperToMax` quantiles are hit under ordinary network variance — no special attacker capability is required. Any unprivileged sync peer can trigger this path. The condition `adjustment = true` is the default. The bug fires on every production node performing IBD or steady-state sync with more than `protect_num` peers.

## Recommendation
Change line 522 from:
```rust
self.timeout_count = self.task_count.saturating_add(num);
```
to:
```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```
This restores the intended semantics: `timeout_count` accumulates across calls and `task_count` is only decremented after more than 2 slow responses.

## Proof of Concept
1. Initialize: `task_count = 32`, `timeout_count = 0`.
2. Slow block response → `remove_by_block` calls `set.decrease(1)`.
3. Line 522: `timeout_count = 32 + 1 = 33`. Since `33 > 2`: `task_count = 31`, `timeout_count = 0`.
4. Next slow response: `timeout_count = 31 + 1 = 32 > 2` → `task_count = 30`, `timeout_count = 0`.
5. After 32 such events: `task_count = 0`. `prune` inserts peer into `disconnect_list`.
6. Under the correct implementation, `timeout_count` would need to accumulate to 3 before any decrement, requiring at least 3 slow responses per `task_count` decrement (~96 total to reach 0).