The bug is confirmed in the actual code. [1](#0-0)  shows `self.timeout_count = self.task_count.saturating_add(num)` at line 522, and [2](#0-1)  confirms `task_count` is initialized to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` and `timeout_count` to `0`. The disconnect logic at [3](#0-2)  and `can_fetch` at [4](#0-3)  are also confirmed.

---

Audit Report

## Title
`DownloadScheduler::decrease` Overwrites `timeout_count` with `task_count`, Bypassing Soft-Penalty Accumulation — (File: sync/src/types/mod.rs)

## Summary
In `sync/src/types/mod.rs` line 522, `decrease` writes `self.timeout_count = self.task_count.saturating_add(num)` instead of `self.timeout_count.saturating_add(num)`. Because `task_count` starts at 32, the result is always ≥ 33, which always exceeds the `> 2` threshold, so `task_count` is decremented and `timeout_count` reset on every single call. The intended two-strike accumulation buffer is completely bypassed, causing premature reduction of concurrent block requests and eventual peer disconnection.

## Finding Description
`DownloadScheduler::default()` initializes `task_count = INIT_BLOCKS_IN_TRANSIT_PER_PEER` (32) and `timeout_count = 0` (`sync/src/types/mod.rs` L489–496).

The buggy function:
```rust
// L521–527
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num); // BUG: should be self.timeout_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

On the first call with `num = 1`: `timeout_count = 32 + 1 = 33 > 2` → `task_count` becomes 31, `timeout_count` reset to 0. On every subsequent call, `task_count` is still ≥ 1 until it reaches 0, so the condition remains always true. `timeout_count` never accumulates — it is overwritten with a value derived from `task_count` and immediately cleared.

`can_fetch()` returns `task_count.saturating_sub(hashes.len())` (L504–506), so each decrement directly reduces the number of concurrent block requests allowed from that peer. When `task_count` reaches 0, the `retain` loop at L692–700 inserts the peer into `disconnect_list` and removes it.

## Impact Explanation
**Low (501–2000 points): Any other important performance improvements for CKB.** The bug miscalibrates the block download scheduler: `task_count` is decremented on every slow block response instead of every third, reducing concurrent block requests per peer three times faster than intended. A peer consistently responding in the slow quantile reaches `task_count = 0` after 32 slow responses instead of ~96+, triggering disconnection. This degrades IBD and normal sync throughput. It does not crash the node, cause consensus deviation, or damage the economy.

## Likelihood Explanation
Any unprivileged sync peer can trigger this path by responding to `GetBlocks` with blocks arriving in the slow time quantile — a routine condition on congested or geographically distant links. No special privileges are required. The path is exercised continuously during sync whenever more than the protected peer count are connected, which is the normal operating state.

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
```rust
#[test]
fn test_decrease_accumulates_timeout_count() {
    let mut sched = DownloadScheduler::default(); // task_count=32, timeout_count=0
    sched.decrease(1); // BUG: timeout_count = 32+1=33 > 2 → task_count becomes 31
    assert_eq!(sched.task_count(), 31); // fires immediately — should still be 32
    sched.decrease(1); // timeout_count = 31+1=32 > 2 → task_count becomes 30
    assert_eq!(sched.task_count(), 30); // fires again — accumulation never happens
}
```

With the fix, the first two `decrease(1)` calls leave `task_count` at 32 (timeout_count accumulates to 1, then 2, neither exceeds 2), and only the third call decrements it.

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
