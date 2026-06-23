### Title
`DownloadScheduler::decrease` Never Accumulates `timeout_count`, Causing Overly Aggressive Peer Disconnection - (File: `sync/src/types/mod.rs`)

### Summary

In `DownloadScheduler::decrease`, the field `timeout_count` is intended to accumulate slow-response events and only reduce `task_count` after crossing a threshold of 2. However, due to a copy-paste error, `timeout_count` is always overwritten with `task_count + num` (which is always >> 2) instead of `timeout_count + num`. As a result, the condition `timeout_count > 2` is always true, and `task_count` is decremented on every single slow block response rather than every 2–3 slow responses as intended.

### Finding Description

In `sync/src/types/mod.rs`, `DownloadScheduler` tracks how many concurrent block download tasks a peer can handle via `task_count` (initialized to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`). The `decrease` function is supposed to implement a soft penalty: accumulate slow-response events in `timeout_count`, and only reduce `task_count` once `timeout_count` exceeds 2. [1](#0-0) 

The bug is on line 522:

```rust
fn decrease(&mut self, num: usize) {
    // BUG: uses self.task_count instead of self.timeout_count
    self.timeout_count = self.task_count.saturating_add(num);
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

Since `task_count` starts at 32, `self.task_count.saturating_add(num)` evaluates to 33 (for `num=1`) or 34 (for `num=2`) — always greater than 2. The condition is therefore always true, `task_count` is decremented on every call, and `timeout_count` is immediately reset to 0. The accumulation mechanism is completely bypassed.

The correct line should be:
```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```

`decrease` is called from `remove_by_block` when a received block's response time falls in the slow quantiles: [2](#0-1) 

When `task_count` reaches 0, `prune()` adds the peer to the disconnect list and removes it: [3](#0-2) 

### Impact Explanation

**Impact: Medium**

- With the bug, `task_count` is decremented on every slow block response. Starting from 32, a peer is disconnected after 32 slow responses.
- Without the bug, `task_count` would only be decremented every 3 slow responses (for `num=1`) or every 2 slow responses (for `num=2`), requiring ~96 or ~64 slow responses respectively before disconnection.
- The penalty is 2–3× more aggressive than intended, causing premature disconnection of legitimate peers experiencing transient network slowness.
- Premature peer disconnection degrades sync throughput and network connectivity, potentially stalling block synchronization.

### Likelihood Explanation

**Likelihood: High**

- The `should_punish` guard (`download_schedulers.len() > protect_num`, where `protect_num = 4`) is satisfied in any normal node with more than 4 sync peers, which is the common case.
- Any block response that falls in the `NormalToUpper` or `UpperToMax` time quantile triggers `decrease`. Transient network jitter, CPU load, or a deliberately slow peer can all trigger this.
- No special privileges are required. Any unprivileged sync peer that responds slowly to `GetBlocks` requests will trigger the bug. [4](#0-3) 

### Recommendation

Fix line 522 in `sync/src/types/mod.rs` to accumulate into `timeout_count` rather than overwrite it with `task_count`:

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

### Proof of Concept

1. A node has 5+ sync peers (so `should_punish = true` and `adjustment = true`).
2. A sync peer deliberately delays block responses so that elapsed time falls in the `NormalToUpper` quantile.
3. Each delayed block response calls `remove_by_block` → `decrease(1)`.
4. Due to the bug, `timeout_count` is set to `task_count + 1` (e.g., 33) on every call, always satisfying `> 2`, so `task_count` is decremented by 1 each time.
5. After 32 such slow responses, `task_count` reaches 0.
6. On the next `prune()` call, the peer is added to `disconnect_list` and disconnected.
7. Without the bug, disconnection would require ~96 slow responses (3× more), giving legitimate slow peers a much larger tolerance window. [1](#0-0) [5](#0-4)

### Citations

**File:** sync/src/types/mod.rs (L482-496)
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

**File:** sync/src/types/mod.rs (L638-700)
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

        let trace = &mut self.trace_number;
        let download_schedulers = &mut self.download_schedulers;
        let states = &mut self.inflight_states;

        let mut remove_key = Vec::new();
        // Since this is a btreemap, with the data already sorted,
        // we don't have to worry about missing points, and we don't need to
        // iterate through all the data each time, just check within tip + 20,
        // with the checkpoint marking possible blocking points, it's enough
        let end = tip + 20;
        for (key, value) in states.iter() {
            if key.number > end {
                break;
            }
            if value.timestamp + BLOCK_DOWNLOAD_TIMEOUT < now {
                if let Some(set) = download_schedulers.get_mut(&value.peer) {
                    set.hashes.remove(key);
                    if should_punish && adjustment {
                        set.punish(2);
                    }
                };
                if !trace.is_empty() {
                    trace.remove(key);
                }
                remove_key.push(key.clone());
                debug!(
                    "prune: remove InflightState: remove {}-{} from {}",
                    key.number, key.hash, value.peer
                );

                if let Some(metrics) = ckb_metrics::handle() {
                    metrics.ckb_inflight_timeout_count.inc();
                }
            }
        }

        for key in remove_key {
            states.remove(&key);
        }

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

**File:** sync/src/types/mod.rs (L798-811)
```rust
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
