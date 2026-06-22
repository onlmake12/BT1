### Title
Broken `timeout_count` Accumulation in `DownloadScheduler::decrease` Causes Hyper-Aggressive Peer Penalty and Premature Disconnection — (`File: sync/src/types/mod.rs`)

---

### Summary

`DownloadScheduler::decrease` overwrites `self.timeout_count` with `self.task_count.saturating_add(num)` instead of accumulating into `self.timeout_count`. Because `task_count` starts at 32 (`INIT_BLOCKS_IN_TRANSIT_PER_PEER`) and `num` is 1 or 2, the result is always > 2, so the guard condition is always true and `task_count` is decremented on **every single slow block response** instead of only after accumulating 2+ slow responses. The `timeout_count` field — intended as a slow-response accumulator — is never actually used as one.

---

### Finding Description

In `sync/src/types/mod.rs`, `DownloadScheduler::decrease` reads:

```rust
fn decrease(&mut self, num: usize) {
    self.timeout_count = self.task_count.saturating_add(num); // BUG
    if self.timeout_count > 2 {
        self.task_count = self.task_count.saturating_sub(1);
        self.timeout_count = 0;
    }
}
```

The correct line should be:

```rust
self.timeout_count = self.timeout_count.saturating_add(num);
```

The wrong operand (`self.task_count` instead of `self.timeout_count`) means the accumulator is never used. Since `task_count` initialises to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`, the expression `32 + 1 = 33` or `32 + 2 = 34` is always > 2, so the branch fires unconditionally on every call.

`decrease` is called from `remove_by_block` whenever a received block's elapsed time falls in the `NormalToUpper` (penalty 1) or `UpperToMax` (penalty 2) quantile:

```rust
TimeQuantile::NormalToUpper => {
    if should_punish { set.decrease(1) }
}
TimeQuantile::UpperToMax => {
    if should_punish { set.decrease(2) }
}
```

`should_punish` is true whenever the number of connected download peers exceeds `protect_num` (default `MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT = 4`). In any normal node with more than 4 peers, every slow block response immediately decrements `task_count` by 1.

When `task_count` reaches 0, `prune()` adds the peer to the disconnect list:

```rust
download_schedulers.retain(|k, v| {
    if v.task_count == 0 {
        disconnect_list.insert(*k);
        false
    } else { true }
});
```

---

### Impact Explanation

**Intended behaviour:** `timeout_count` accumulates slow-response events; `task_count` is only decremented after 3+ accumulated slow responses (threshold > 2), giving peers a grace window before being penalised.

**Actual behaviour:** Every slow block response immediately decrements `task_count` by 1. Starting from 32, a peer reaches `task_count = 0` after only 32 slow responses instead of the intended ~96+. The penalty is at minimum 3× more aggressive than designed.

Concrete consequences:
- A peer that is legitimately slow (high-latency link, loaded CPU) is disconnected ~3× faster than the protocol intends.
- A victim node with few peers that loses connections prematurely is left with a reduced peer set, increasing its exposure to eclipse attacks and sync stalls.
- A malicious sync peer can deliberately pace block responses into the slow quantile (above `normal_time`, below `BLOCK_DOWNLOAD_TIMEOUT = 30 s`) to force the victim to exhaust its `task_count` for that peer in 32 responses and trigger disconnection, then reconnect and repeat — cycling the victim's peer slots.

---

### Likelihood Explanation

- Any unprivileged peer reachable over the P2P network can trigger this path by responding to `GetBlocks` requests with valid blocks delivered slowly.
- No special keys, operator access, or majority hashpower is required.
- The condition `should_punish` (peer count > 4) is satisfied on any normally-connected mainnet node.
- The `adjustment` flag defaults to `true`, keeping the code path active.
- The bug fires on every slow response, making it trivially repeatable.

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

This restores the intended accumulator semantics: `task_count` is only decremented after the accumulated slow-response count exceeds 2, giving peers the designed grace window before penalisation.

---

### Proof of Concept

**Root cause lines:** [1](#0-0) 

**Constants confirming `task_count` initial value (32) always exceeds the threshold (2):** [2](#0-1) 

**Call sites that feed slow-response events into `decrease`:** [3](#0-2) 

**Disconnection triggered when `task_count` reaches 0:** [4](#0-3) 

**Walkthrough:**

1. Attacker connects as a sync peer to a victim CKB node (> 4 total peers, so `should_punish = true`, `adjustment = true`).
2. Victim sends `GetBlocks` requests; attacker responds with valid blocks, each taking between `normal_time` (≈1250 ms) and `BLOCK_DOWNLOAD_TIMEOUT` (30 000 ms) — slow enough to hit `NormalToUpper` or `UpperToMax` quantile, fast enough to avoid the hard timeout prune path.
3. Each such response triggers `remove_by_block` → `set.decrease(1)`.
4. Inside `decrease`: `timeout_count = 32 + 1 = 33 > 2` → `task_count` decremented to 31, `timeout_count` reset to 0.
5. After 32 such responses, `task_count = 0`.
6. Next `prune()` call adds the peer to `disconnect_list` and disconnects it.
7. Attacker reconnects; victim's peer slot is recycled. Repeated cycling degrades the victim's effective peer set and sync throughput.

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

**File:** sync/src/types/mod.rs (L797-811)
```rust
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

**File:** util/constant/src/sync.rs (L14-16)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
/// Maximum number of download blocks that can be requested at one time
pub const MAX_BLOCKS_IN_TRANSIT_PER_PEER: usize = 128;
```
