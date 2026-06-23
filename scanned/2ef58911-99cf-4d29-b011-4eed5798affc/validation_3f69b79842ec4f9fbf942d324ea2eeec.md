### Title
Wrong Variable in `DownloadScheduler::decrease` Discards Accumulated Timeout State, Causing Overly Aggressive Peer Penalization — (`sync/src/types/mod.rs`)

---

### Summary

`DownloadScheduler::decrease` assigns `self.task_count.saturating_add(num)` to `self.timeout_count` instead of `self.timeout_count.saturating_add(num)`. This discards the accumulated timeout history on every call, exactly mirroring the Zivoe EMA bug where an initial data point is silently thrown away because the wrong counter value is used in the smoothing formula. The intended damping threshold of 3 accumulated slow-response events before penalizing a peer is bypassed entirely: `task_count` is decremented on every single slow block response.

---

### Finding Description

`DownloadScheduler` is the per-peer block-download concurrency controller used during IBD and post-IBD sync. It tracks how many blocks can be requested from a peer simultaneously (`task_count`, initialized to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`) and a running count of slow responses (`timeout_count`, initialized to `0`).

The intended design of `decrease` is to accumulate slow-response events in `timeout_count` and only reduce `task_count` once the accumulated count exceeds 2 — a damping mechanism to avoid over-reacting to transient latency spikes.

The actual code:

```rust
// sync/src/types/mod.rs  line 521-527
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num);  // BUG
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

Line 522 reads `self.task_count` instead of `self.timeout_count`. Because `task_count` starts at 32 and `num` is always 1 or 2 (passed from `remove_by_block`), `timeout_count` is set to at least 33 on every call. The guard `if self.timeout_count > 2` is therefore always true, so `task_count` is decremented and `timeout_count` is reset to 0 on every invocation. The accumulation state is discarded before it can ever matter.

The correct line should be:

```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```

`decrease` is called from `remove_by_block` when a completed block download falls in the `NormalToUpper` (slow) or `UpperToMax` (very slow) time quantile:

```rust
// sync/src/types/mod.rs  line 801-810
TimeQuantile::NormalToUpper => {
    if should_punish { set.decrease(1) }
}
TimeQuantile::UpperToMax => {
    if should_punish { set.decrease(2) }
}
``` [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

With the bug, every slow block response immediately decrements `task_count` by 1 (or 2 for very slow). The intended behavior is to tolerate up to 2 slow responses before penalizing. The effective penalty rate is therefore 3× more aggressive than designed.

When `task_count` reaches 0, the peer is evicted:

```rust
// sync/src/types/mod.rs  line 692-700
download_schedulers.retain(|k, v| {
    if v.task_count == 0 {
        disconnect_list.insert(*k);
        false
    } else {
        true
    }
});
``` [4](#0-3) 

A legitimate peer that experiences a brief latency spike (e.g., due to CPU load or network jitter) will have its `task_count` driven to 0 and be disconnected far sooner than the protocol intends. Starting from `task_count = 32`, only 32 slow responses are needed instead of the intended ~96 (32 × 3). This degrades sync throughput and causes unnecessary peer churn, weakening the node's connectivity during IBD.

---

### Likelihood Explanation

The code path is exercised continuously during normal block synchronization. Any peer whose response latency exceeds the `TimeAnalyzer`'s `normal_time` threshold (initially 1250 ms) triggers `decrease`. Network jitter, CPU-heavy block validation, or a moderately loaded remote peer can all produce this. No special attacker capability is required; an unprivileged sync peer that sends block responses slightly above the latency threshold will trigger the bug on every response. The `adjustment` flag must be `true` (the default during IBD) and `should_punish` must be `true` (requires more than `MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT = 4` connected download peers). [5](#0-4) [6](#0-5) 

---

### Recommendation

Change line 522 in `sync/src/types/mod.rs` from:

```rust
self.timeout_count = self.task_count.saturating_add(num);
```

to:

```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```

This restores the intended damping behavior: `task_count` is only decremented after `timeout_count` accumulates more than 2 slow-response events, and `timeout_count` is reset to 0 only at that point.

---

### Proof of Concept

**Initial state** (from `Default`):
- `task_count = 32` (`INIT_BLOCKS_IN_TRANSIT_PER_PEER`)
- `timeout_count = 0`

**Call 1: `decrease(1)`** (one slow response)
- Buggy: `timeout_count = 32 + 1 = 33`; `33 > 2` → `task_count = 31`, `timeout_count = 0`
- Correct: `timeout_count = 0 + 1 = 1`; `1 > 2` is false → `task_count` unchanged

**Call 2: `decrease(1)`**
- Buggy: `timeout_count = 31 + 1 = 32`; `32 > 2` → `task_count = 30`, `timeout_count = 0`
- Correct: `timeout_count = 1 + 1 = 2`; `2 > 2` is false → `task_count` unchanged

**Call 3: `decrease(1)`**
- Buggy: `timeout_count = 30 + 1 = 31`; `31 > 2` → `task_count = 29`, `timeout_count = 0`
- Correct: `timeout_count = 2 + 1 = 3`; `3 > 2` → `task_count = 31`, `timeout_count = 0`

After 3 slow responses: buggy code has `task_count = 29`; correct code has `task_count = 31`. After 32 slow responses the buggy code reaches `task_count = 0` and the peer is disconnected. The correct code would require ~96 slow responses to reach the same outcome. [7](#0-6)

### Citations

**File:** sync/src/types/mod.rs (L482-532)
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
}
```

**File:** sync/src/types/mod.rs (L545-556)
```rust
impl Default for InflightBlocks {
    fn default() -> Self {
        InflightBlocks {
            download_schedulers: HashMap::default(),
            inflight_states: BTreeMap::default(),
            trace_number: HashMap::default(),
            restart_number: 0,
            time_analyzer: TimeAnalyzer::default(),
            adjustment: true,
            protect_num: MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT,
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

**File:** sync/src/types/mod.rs (L799-811)
```rust
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

**File:** util/constant/src/sync.rs (L14-16)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
/// Maximum number of download blocks that can be requested at one time
pub const MAX_BLOCKS_IN_TRANSIT_PER_PEER: usize = 128;
```

**File:** util/constant/src/sync.rs (L36-36)
```rust
pub const MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT: usize = 4;
```
