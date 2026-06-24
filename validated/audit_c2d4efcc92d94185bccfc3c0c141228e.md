The code is confirmed. [1](#0-0)  shows `self.task_count.saturating_add(num)` on line 522 instead of `self.timeout_count.saturating_add(num)`. `task_count` initializes to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32` [2](#0-1)  and `timeout_count` initializes to 0 [3](#0-2) , making the `> 2` guard fire unconditionally. The disconnect path at lines 692–700 is confirmed. [4](#0-3) 

---

Audit Report

## Title
`DownloadScheduler::decrease` Uses Wrong Variable, Bypassing Timeout Accumulation Threshold — (File: sync/src/types/mod.rs)

## Summary
In `DownloadScheduler::decrease`, line 522 assigns `self.timeout_count = self.task_count.saturating_add(num)` instead of `self.timeout_count = self.timeout_count.saturating_add(num)`. Because `task_count` is initialized to 32 and `num` ∈ {1, 2}, the expression always evaluates to ≥ 33, making the `> 2` guard fire unconditionally on every call. The intended three-event accumulation before penalizing a peer is completely bypassed, causing peers to be disconnected after ~32 slow responses instead of the intended ~96, degrading block-download throughput during sync.

## Finding Description
`DownloadScheduler` tracks concurrent block-download task allowance per peer (`task_count`, initialized to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`, max 128). When `remove_by_block` classifies a block response as `NormalToUpper` or `UpperToMax`, it calls `decrease(1)` or `decrease(2)` respectively. The design intent is to accumulate slow events in `timeout_count` and only decrement `task_count` after `timeout_count > 2`.

The actual code at `sync/src/types/mod.rs:521–527`:
```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num); // wrong variable
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```
Since `task_count` starts at 32 and `num` ∈ {1, 2}, `task_count + num` is always ≥ 33 > 2. The guard fires unconditionally on every call. `task_count` is decremented on every slow-response event, and `timeout_count` is reset to 0 immediately each time, so no accumulation ever occurs. When `task_count` reaches 0, `prune()` at lines 692–700 adds the peer to `disconnect_list`.

## Impact Explanation
Peers are disconnected after 32 consecutive slow responses instead of the intended ~96 (one decrement per 3 events). During IBD, this causes the node to shed sync peers prematurely under normal variable-latency conditions, degrading block-download throughput. In the worst case, if remaining peers are also slow, sync can stall. This is a concrete, continuously triggered performance regression affecting sync reliability. This matches the **Low (501–2000 points)** impact class: an important performance improvement for CKB.

## Likelihood Explanation
No attacker capability is required. Any peer that responds to `GetBlocks` in the `NormalToUpper` or `UpperToMax` time band triggers `decrease`. Variable network latency, congested links, and high-load peers are normal occurrences on mainnet. The code path is exercised continuously during sync. The bug fires automatically for any legitimately slow peer.

## Recommendation
Change `sync/src/types/mod.rs` line 522 from:
```rust
self.timeout_count = self.task_count.saturating_add(num);
```
to:
```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```
This restores the intended accumulation: `task_count` is only decremented after `timeout_count` has accumulated past the threshold of 2.

## Proof of Concept
Given `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`:
1. Peer P connects; `task_count = 32`, `timeout_count = 0`.
2. P returns a block in the `NormalToUpper` band → `decrease(1)` called.
3. `timeout_count = 32 + 1 = 33`; `33 > 2` → `task_count = 31`, `timeout_count = 0`.
4. Next slow response: `timeout_count = 31 + 1 = 32`; `32 > 2` → `task_count = 30`, `timeout_count = 0`.
5. After 32 such events: `task_count = 0` → peer disconnected in next `prune()`.

With the correct code, step 3 sets `timeout_count = 0 + 1 = 1`; `1 > 2` is false; `task_count` stays at 32. Only after 3 accumulated slow responses does `task_count` decrement once, requiring ~96 slow responses to reach 0. A unit test can be written directly against `DownloadScheduler::decrease` to assert that `task_count` does not change after a single `decrease(1)` call from the initial state.

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

**File:** util/constant/src/sync.rs (L14-14)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
```
