### Title
`DownloadScheduler::decrease` Uses Wrong Variable, Causing Task Count to Drop on Every Timeout — (`sync/src/types/mod.rs`)

---

### Summary

In `sync/src/types/mod.rs`, the `DownloadScheduler::decrease` function accumulates the timeout counter using `self.task_count` instead of `self.timeout_count`. Because `task_count` starts at 32 (`INIT_BLOCKS_IN_TRANSIT_PER_PEER`), the condition `self.timeout_count > 2` is always true on every call, so `task_count` is decremented on **every single block-download timeout** rather than after the intended threshold of 3 consecutive timeouts. A malicious sync peer that consistently fails to deliver requested blocks can drive the victim's per-peer `task_count` to zero, at which point `can_fetch()` returns 0 and the node stops requesting blocks from that peer entirely.

---

### Finding Description

`DownloadScheduler` is the per-peer sliding-window controller that governs how many blocks the synchronizer may have in-flight at once. [1](#0-0) 

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num);   // ← BUG
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

The first line should read `self.timeout_count = self.timeout_count.saturating_add(num)`. Because `self.task_count` is used instead, `timeout_count` is reset to `task_count + num` on every call. Since `task_count` is initialised to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`: [2](#0-1) 

…the expression `32 + 1 = 33 > 2` is always true, so `task_count` is decremented by 1 and `timeout_count` is reset to 0 on **every** invocation of `decrease`. The intended "slow-down only after 3 consecutive timeouts" logic is completely bypassed.

The `can_fetch` method gates all block requests: [3](#0-2) 

```rust
fn can_fetch(&self) -> usize {
    self.task_count.saturating_sub(self.hashes.len())
}
```

When `task_count` reaches 0, `can_fetch()` returns 0 and the node issues no further block requests to that peer.

The `DownloadScheduler` is used in `BlockFetcher` to determine how many blocks to request per peer: [4](#0-3) 

---

### Impact Explanation

A malicious sync peer (any unprivileged inbound or outbound connection) that consistently fails to respond to `GetBlocks` messages will trigger repeated calls to `decrease`. Due to the bug, each timeout decrements `task_count` by 1. After approximately 32 timeouts the victim's `task_count` for that peer reaches 0, and the synchronizer stops requesting blocks from it. During Initial Block Download (IBD), where the node may rely on a small set of outbound peers, an adversary controlling one or more of those peers can stall or severely delay chain synchronization. The node cannot distinguish this from a legitimately slow peer and will not automatically recover unless `increase` is triggered by a successful block delivery — which the attacker withholds.

---

### Likelihood Explanation

Any unprivileged peer reachable over the P2P network can exploit this. The attacker only needs to accept connections (or be connected to) and then silently drop `Block` responses. No special privilege, key material, or majority hash power is required. The `BLOCK_DOWNLOAD_TIMEOUT` is 30 seconds: [5](#0-4) 

so the attacker needs roughly 32 × 30 s ≈ 16 minutes of non-response to exhaust `task_count` from its initial value of 32.

---

### Recommendation

Replace the wrong variable in the accumulation:

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

Additionally, consider adding a lower bound (e.g., 1) to `task_count` so that a peer is never completely starved of requests, and add a peer-disconnect policy when `task_count` reaches a minimum threshold after repeated timeouts.

---

### Proof of Concept

1. Attacker runs a node that speaks the CKB Sync protocol but never sends `Block` responses.
2. Victim connects to attacker (outbound) or attacker connects to victim (inbound).
3. Victim's `BlockFetcher` sends `GetBlocks` to attacker; attacker ignores them.
4. After `BLOCK_DOWNLOAD_TIMEOUT` (30 s) each inflight entry times out, calling `decrease(1)`.
5. Due to the bug, each call sets `timeout_count = task_count + 1 > 2`, so `task_count` decrements by 1 and `timeout_count` resets to 0.
6. After ~32 cycles (≈16 min), `task_count = 0`; `can_fetch()` returns 0; victim issues no further block requests to attacker.
7. If the attacker is the victim's only (or primary) sync peer during IBD, chain synchronization halts. [6](#0-5)

### Citations

**File:** sync/src/types/mod.rs (L489-531)
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
```

**File:** util/constant/src/sync.rs (L14-16)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
/// Maximum number of download blocks that can be requested at one time
pub const MAX_BLOCKS_IN_TRANSIT_PER_PEER: usize = 128;
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
