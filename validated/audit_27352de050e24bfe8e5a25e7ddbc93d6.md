Audit Report

## Title
`DownloadScheduler::decrease` Accumulates `task_count` Instead of `timeout_count`, Bypassing Timeout Threshold — (`sync/src/types/mod.rs`)

## Summary
In `sync/src/types/mod.rs`, the `decrease` method of `DownloadScheduler` incorrectly assigns `self.timeout_count = self.task_count.saturating_add(num)` instead of `self.timeout_count = self.timeout_count.saturating_add(num)`. Because `task_count` is initialized to 32 (`INIT_BLOCKS_IN_TRANSIT_PER_PEER`), the condition `self.timeout_count > 2` is always satisfied on every invocation, causing `task_count` to be decremented on every block-download timeout rather than only after 3 consecutive timeouts. A malicious peer that silently drops `Block` responses can exhaust `task_count` to zero in approximately 32 timeout cycles (~16 minutes), after which `can_fetch()` returns 0 and the victim node stops requesting blocks from that peer entirely.

## Finding Description
The confirmed buggy code at line 522:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num);  // ← uses task_count, not timeout_count
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
``` [1](#0-0) 

`task_count` is initialized to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`: [2](#0-1) [3](#0-2) 

On every call with `num = 1`, `timeout_count` is set to `32 + 1 = 33`, which is always `> 2`. The intended accumulation logic (increment `timeout_count` across calls, only penalize after 3 consecutive timeouts) is completely bypassed. Instead, `task_count` is decremented by 1 and `timeout_count` is reset to 0 on **every** invocation.

`can_fetch()` gates all block requests to a peer: [4](#0-3) 

When `task_count` reaches 0, `can_fetch()` returns 0. This value is consumed in `BlockFetcher` to determine how many blocks to request: [5](#0-4) 

No existing guard prevents `task_count` from reaching 0 — `saturating_sub` silently floors at 0 with no recovery mechanism unless `increase` is triggered by a successful block delivery (which the attacker withholds).

## Impact Explanation
A malicious peer can cause the victim node to permanently stop requesting blocks from it by silently ignoring `GetBlocks` messages. During Initial Block Download (IBD), where a node may rely on a small set of outbound peers, an attacker controlling one or more of those peers can stall chain synchronization entirely. This matches the **High** impact class: a vulnerability that could cause CKB network congestion or severely impair a node's ability to sync, achievable with minimal cost by any unprivileged peer.

## Likelihood Explanation
Any peer reachable over the P2P network can exploit this — no special privileges, key material, or hash power required. The attacker only needs to accept connections and silently drop `Block` responses. With `BLOCK_DOWNLOAD_TIMEOUT = 30s`: [6](#0-5) 

approximately 32 × 30s ≈ 16 minutes of non-response exhausts `task_count` from its initial value of 32. The attack is repeatable and requires no victim interaction beyond the normal sync protocol.

## Recommendation
Replace the wrong variable in the accumulation line:

```rust
fn decrease(&mut self, num: usize) {
    // Fix: accumulate into timeout_count, not task_count
    self.timeout_count = self.timeout_count.saturating_add(num);
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

Additionally, enforce a minimum floor (e.g., 1) on `task_count` so a peer is never completely starved of requests, and consider adding a peer-disconnect policy when `task_count` falls below a minimum threshold after repeated timeouts.

## Proof of Concept
1. Attacker runs a node speaking the CKB Sync protocol but never sends `Block` responses.
2. Victim connects to attacker (outbound) or attacker connects to victim (inbound).
3. Victim's `BlockFetcher` sends `GetBlocks`; attacker ignores them.
4. After `BLOCK_DOWNLOAD_TIMEOUT` (30s), each in-flight entry times out, calling `decrease(1)`.
5. Due to the bug: `timeout_count = task_count + 1 = 33 > 2` → `task_count -= 1`, `timeout_count = 0`.
6. After ~32 cycles (~16 min), `task_count = 0`; `can_fetch()` returns 0; victim issues no further block requests to attacker.
7. If attacker is the victim's only or primary sync peer during IBD, chain synchronization halts with no automatic recovery.

A unit test can confirm this directly by constructing a `DownloadScheduler::default()` and calling `decrease(1)` 32 times, asserting that `task_count` reaches 0 — which the buggy code produces but the fixed code does not.

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

**File:** util/constant/src/sync.rs (L14-14)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
```

**File:** util/constant/src/sync.rs (L48-48)
```rust
pub const BLOCK_DOWNLOAD_TIMEOUT: u64 = 30 * 1000; // 30s
```

**File:** sync/src/synchronizer/block_fetcher.rs (L219-222)
```rust
        let n_fetch = min(
            end.saturating_sub(start) as usize + 1,
            state.read_inflight_blocks().peer_can_fetch_count(self.peer),
        );
```
