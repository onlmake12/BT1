Audit Report

## Title
`DownloadScheduler::decrease` Uses `task_count` Instead of `timeout_count`, Bypassing Accumulation Threshold - (File: `sync/src/types/mod.rs`)

## Summary
In `DownloadScheduler::decrease`, line 522 writes `self.task_count.saturating_add(num)` into `self.timeout_count` instead of `self.timeout_count.saturating_add(num)`. Because `task_count` is initialized to 32 (`INIT_BLOCKS_IN_TRANSIT_PER_PEER`), the result is always ≥ 33, which always satisfies `> 2`, so `task_count` is decremented on every slow block response rather than every 2–3 as intended. This makes peer disconnection 2–3× more aggressive than designed, prematurely evicting legitimate peers experiencing transient network slowness.

## Finding Description
`DownloadScheduler` tracks per-peer download concurrency via `task_count` (default 32) and is meant to accumulate slow-response events in `timeout_count` before penalizing. The faulty code at `sync/src/types/mod.rs:522`:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num); // BUG: should be self.timeout_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

Since `task_count` starts at 32, `self.task_count.saturating_add(1)` = 33 and `self.task_count.saturating_add(2)` = 34 — both always exceed 2. The accumulation guard is permanently bypassed: `task_count` is decremented and `timeout_count` reset to 0 on every single call.

`decrease` is called from `remove_by_block` at lines 801–810 when `time_analyzer.push_time(elapsed)` returns `NormalToUpper` (calls `decrease(1)`) or `UpperToMax` (calls `decrease(2)`), gated by `should_punish = self.download_schedulers.len() > self.protect_num`. Once `task_count` reaches 0, `prune()` at lines 692–700 inserts the peer into `disconnect_list` and removes it. [1](#0-0) [2](#0-1) [3](#0-2) 

## Impact Explanation
**Low (501–2000 points) — Important performance improvement for CKB.**

The bug does not crash a node, cause consensus deviation, or damage the economy. Its concrete effect is that the per-peer `task_count` penalty is applied 2–3× more frequently than intended, causing legitimate peers experiencing transient slowness to be disconnected after ~32 slow responses instead of ~64–96. This degrades sync throughput and peer diversity but does not meet the threshold for Medium (state storage), High (node crash / network congestion), or Critical impacts. It fits squarely within "any other important performance improvements for CKB."

## Likelihood Explanation
The precondition (`download_schedulers.len() > protect_num`, i.e., >4 sync peers) is satisfied on any normally operating CKB node. Any block response that lands in the `NormalToUpper` or `UpperToMax` time quantile — caused by ordinary network jitter, CPU load spikes, or a deliberately slow peer — triggers `decrease`. No special privileges are required; any unprivileged sync peer can trigger this path simply by responding slowly to `GetBlocks` requests.

## Recommendation
Fix line 522 to accumulate into `timeout_count` rather than overwrite it with `task_count`:

```diff
 fn decrease(&mut self, num: usize) {
-    self.timeout_count = self.task_count.saturating_add(num);
+    self.timeout_count = self.timeout_count.saturating_add(num);
     if self.timeout_count > 2 {
         self.task_count = self.task_count.saturating_sub(1);
         self.timeout_count = 0;
     }
 }
```

## Proof of Concept
1. Run a CKB node with ≥5 sync peers so `should_punish = true` and `adjustment = true`.
2. Have one peer consistently respond to `GetBlocks` with delays placing elapsed time in the `NormalToUpper` quantile.
3. Observe via logging or metrics that `task_count` for that peer decrements by 1 on every such response (not every 3rd).
4. After 32 such responses, `task_count` reaches 0; the next `prune()` call disconnects the peer.
5. A unit test can directly call `decrease(1)` 32 times on a fresh `DownloadScheduler` and assert `task_count == 0`, confirming the accumulation guard is never effective.

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

**File:** sync/src/types/mod.rs (L785-811)
```rust
    pub fn remove_by_block(&mut self, block: BlockNumberAndHash) -> bool {
        let should_punish = self.download_schedulers.len() > self.protect_num;
        let download_schedulers = &mut self.download_schedulers;
        let trace = &mut self.trace_number;
        let time_analyzer = &mut self.time_analyzer;
        let adjustment = self.adjustment;
        self.inflight_states
            .remove(&block)
            .map(|state| {
                let elapsed = unix_time_as_millis().saturating_sub(state.timestamp);
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
