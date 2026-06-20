### Title
`DownloadScheduler::decrease` Incorrectly Updates `timeout_count`, Causing Over-Aggressive Peer Penalization During Block Sync - (File: `sync/src/types/mod.rs`)

---

### Summary

The `DownloadScheduler::decrease` function in the sync state machine assigns `self.task_count.saturating_add(num)` to `self.timeout_count` instead of `self.timeout_count.saturating_add(num)`. This means the accumulation guard (`timeout_count`) is never actually accumulated — it is always set to a value larger than the threshold, so `task_count` is decremented on every single slow block response rather than only after multiple slow responses. This causes the node to over-penalize peers that respond slowly, reducing their download capacity and triggering premature disconnection faster than the protocol intends.

---

### Finding Description

In `sync/src/types/mod.rs`, the `DownloadScheduler` struct tracks two related state variables:

- `task_count`: the number of concurrent block download slots allowed for a peer (starts at `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`)
- `timeout_count`: an accumulator intended to require multiple slow responses before penalizing `task_count` [1](#0-0) 

The `decrease` function is supposed to accumulate `timeout_count` and only reduce `task_count` after the accumulator exceeds 2: [2](#0-1) 

The bug is on line 522:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num);  // BUG: should be self.timeout_count.saturating_add(num)
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

Because `task_count` starts at 32 and `num` is 1 or 2, `self.task_count.saturating_add(num)` is always ≥ 33, which is always `> 2`. The `if` branch fires unconditionally on every call, decrementing `task_count` by 1 and resetting `timeout_count` to 0. The accumulator is never actually accumulated — it is overwritten with a stale, unrelated value (`task_count + num`) on every invocation.

`decrease` is called from `remove_by_block` when a block response is slow: [3](#0-2) 

When `task_count` reaches 0, `prune()` disconnects the peer: [4](#0-3) 

---

### Impact Explanation

With the bug, every slow block response (classified as `NormalToUpper` or `UpperToMax`) immediately decrements `task_count` by 1. Starting from 32, a peer is disconnected after 32 slow responses. Without the bug, `timeout_count` would accumulate and `task_count` would only be decremented after every 3 slow responses — requiring ~96 slow responses before disconnection. The node therefore:

1. Reduces its concurrent download capacity from slow-but-valid peers 3× faster than intended.
2. Disconnects legitimate peers prematurely, degrading sync throughput.
3. Is more susceptible to isolation: if an adversary can introduce network latency between the victim and its honest peers (e.g., via congestion or a relay), those peers are disconnected faster than the protocol intends, narrowing the victim's peer set.

---

### Likelihood Explanation

Any sync peer that responds to `GetBlocks` with block data in the `NormalToUpper` or `UpperToMax` time range (i.e., slower than `normal_time` but not timing out) triggers `decrease()`. This is reachable by any unprivileged P2P block-download peer without any special capability. On congested or geographically distant networks, legitimate peers routinely fall into this timing range, making the bug trigger in normal operation. [5](#0-4) 

---

### Recommendation

Fix the assignment in `decrease` to accumulate `timeout_count` rather than overwrite it with `task_count + num`:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.timeout_count.saturating_add(num); // was: self.task_count.saturating_add(num)
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

This restores the intended behavior: `task_count` is only decremented after the accumulator exceeds the threshold of 2, matching the graduated penalty design.

---

### Proof of Concept

1. Connect a sync peer to a CKB node.
2. Respond to every `GetBlocks` request with a valid block, but with a delay between `normal_time` (1250 ms) and `low_time` (1500 ms) — slow enough to be classified as `NormalToUpper`.
3. Each response triggers `remove_by_block` → `decrease(1)`.
4. Due to the bug, `task_count` is decremented by 1 on every response.
5. After 32 such responses, `task_count` reaches 0.
6. On the next `prune()` call, the peer is disconnected — 3× faster than the intended design. [6](#0-5) [7](#0-6)

### Citations

**File:** sync/src/types/mod.rs (L482-497)
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
```

**File:** sync/src/types/mod.rs (L512-531)
```rust
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

**File:** sync/src/types/mod.rs (L638-650)
```rust
    pub fn prune(&mut self, tip: BlockNumber) -> HashSet<PeerIndex> {
        let now = unix_time_as_millis();
        let mut disconnect_list = HashSet::new();
        // Since statistics are currently disturbed by the processing block time, when the number
        // of transactions increases, the node will be accidentally evicted.
        //
        // Especially on machines with poor CPU performance, the node connection will be frequently
        // disconnected due to statistics.
        //
        // In order to protect the decentralization of the network and ensure the survival of low-performance
        // nodes, the penalty mechanism will be closed when the number of download nodes is less than the number of protected nodes
        let should_punish = self.download_schedulers.len() > self.protect_num;
        let adjustment = self.adjustment;
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

**File:** sync/src/types/mod.rs (L785-819)
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
                    }
                    if !trace.is_empty() {
                        trace.remove(&block);
                    }
                };
            })
            .is_some()
    }
```
