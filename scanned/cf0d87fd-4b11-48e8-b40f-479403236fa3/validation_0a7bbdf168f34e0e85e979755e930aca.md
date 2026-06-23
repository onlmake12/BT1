### Title
`HeadersSyncController::is_timeout` Falsely Evicts Syncing Peers When `better_tip_ts` Decreases Due to Non-Monotonic Block Timestamps — (`sync/src/types/mod.rs`)

---

### Summary

`HeadersSyncController::is_timeout()` measures header-sync progress by comparing the current `better_tip_ts` (the timestamp of the best-known tip header) against a stored baseline `last_updated_tip_ts`. The function implicitly assumes `better_tip_ts` is monotonically non-decreasing. However, CKB block timestamps are **not** strictly monotonically increasing — they only need to exceed the median of the previous 37 blocks. A malicious peer with valid PoW can send headers with high total difficulty but low timestamps, causing `better_tip_ts` to decrease. When this happens, `saturating_sub` silently returns 0, which is then compared against a positive expected-progress value, triggering a false timeout that disconnects every peer currently syncing headers.

---

### Finding Description

In `sync/src/types/mod.rs`, `HeadersSyncController::is_timeout()` computes instantaneous sync progress as:

```rust
let synced_since_last_updated = now_tip_ts.saturating_sub(self.last_updated_tip_ts);
``` [1](#0-0) 

`now_tip_ts` is the timestamp of `better_tip_header`, computed in `sync/src/synchronizer/mod.rs`:

```rust
fn better_tip_header(&self) -> HeaderIndexView {
    ...
    if total_difficulty > *best_known.total_difficulty() {
        (header, total_difficulty).into()
    } else {
        best_known   // shared_best_header wins
    }
}
``` [2](#0-1) 

`better_tip_header` is computed **once per `eviction()` call** and fed to every peer's `is_timeout()` check:

```rust
let better_tip_ts = better_tip_header.timestamp();
if let Some(is_timeout) = controller.is_timeout(better_tip_ts, now) {
    if is_timeout {
        eviction.push(*peer);
``` [3](#0-2) 

**The flaw**: CKB's `TimestampVerifier` only requires a block's timestamp to exceed the **median** of the previous 37 blocks, not the previous block's timestamp:

```rust
let min = self.data_loader.block_median_time(...);
if self.header.timestamp() <= min {
    return Err(TimestampError::BlockTimeTooOld { ... });
}
``` [4](#0-3) 

This means a chain can have a tip timestamp **lower** than a previously seen tip timestamp. When `now_tip_ts < last_updated_tip_ts`, `saturating_sub` returns `0`. The check:

```rust
if synced_since_last_updated < expected_since_last_updated / tolerable_bias {
    Some(true)  // ← false timeout, peer disconnected
``` [5](#0-4) 

fires unconditionally because `0 < expected_since_last_updated / tolerable_bias` whenever any wall-clock time has elapsed. The same `better_tip_ts` is used for **all** peers in the same `eviction()` loop, so a single decrease in `better_tip_ts` disconnects every peer that has an active `headers_sync_controller`. [6](#0-5) 

---

### Impact Explanation

Any peer currently syncing headers (i.e., with an active `HeadersSyncController`) is falsely evicted. During IBD this is especially severe: the node is forced to drop its honest sync peer(s) and must reconnect, potentially to the attacker. This enables a targeted **eclipse / sync-stall attack**: the attacker can repeatedly trigger the condition to prevent the victim from completing IBD, or to force it to sync exclusively from attacker-controlled peers.

---

### Likelihood Explanation

The attacker must be a connected peer and must supply headers with valid PoW whose tip timestamp is lower than the victim's current `last_updated_tip_ts`. Because CKB's timestamp rule only enforces `timestamp > median(prev 37)`, a miner can legally produce a chain where each block's timestamp is set to just above the rolling median, yielding a tip timestamp far below real-world time. A peer that has mined even a modest side-chain with high compact-target difficulty and deliberately suppressed timestamps can satisfy this condition. The `shared_best_header` is updated whenever a peer's announced header has higher total difficulty than the current best:

```rust
if total_difficulty > *best_known.total_difficulty() { ... }
``` [7](#0-6) 

so the attacker only needs to exceed the current `shared_best_header`'s total difficulty — not the full honest chain's difficulty. This is a meaningful but not prohibitive bar for a well-resourced adversary.

---

### Recommendation

Before computing `synced_since_last_updated`, detect and handle a decreasing `now_tip_ts`. If the tip timestamp has regressed, reset the baseline rather than treating the regression as zero progress:

```rust
if now_tip_ts < self.last_updated_tip_ts {
    // Tip timestamp regressed (e.g., reorg or shared_best_header update).
    // Reset baseline to avoid a spurious timeout.
    self.last_updated_ts = now;
    self.last_updated_tip_ts = now_tip_ts;
    return Some(false);
}
let synced_since_last_updated = now_tip_ts - self.last_updated_tip_ts;
```

The same guard should be applied to the `synced_since_started` path at line 242. [8](#0-7) 

---

### Proof of Concept

1. Victim node **V** is in IBD, syncing headers from honest peer **A**. After two or more `inspect_window` (2-minute) intervals of acceptable progress, `HeadersSyncController.last_updated_tip_ts = T1` (a recent blockchain timestamp, e.g., a few hours ago in blockchain time).

2. Malicious peer **M** connects to **V** and sends a `SendHeaders` message containing a chain of headers with:
   - Valid Eaglesong PoW.
   - Total difficulty > current `shared_best_header` total difficulty.
   - Tip timestamp `T2` where `T2 < T1` (achieved by setting each block's timestamp to just above the rolling 37-block median, keeping timestamps artificially low).

3. `HeadersProcess::execute()` validates the headers (PoW passes, median-time check passes because each header's timestamp exceeds its own 37-block median), then calls `may_set_best_known_header`, updating `shared_best_header` to M's tip with timestamp `T2`.

4. Next periodic `eviction()` call: `better_tip_header()` returns M's header (higher total difficulty), so `better_tip_ts = T2`.

5. For peer **A**'s `HeadersSyncController`: `synced_since_last_updated = T2.saturating_sub(T1) = 0`. Since `spent_since_last_updated >= inspect_window`, `expected_since_last_updated / tolerable_bias > 0`, so `is_timeout()` returns `Some(true)`.

6. **A** is disconnected. **V** is now forced to sync from **M** alone.

### Citations

**File:** sync/src/types/mod.rs (L224-231)
```rust
                let synced_since_last_updated = now_tip_ts.saturating_sub(self.last_updated_tip_ts);
                let expected_since_last_updated =
                    expected_headers_per_sec * spent_since_last_updated * POW_INTERVAL / 1000;

                if synced_since_last_updated < expected_since_last_updated / tolerable_bias {
                    // if instantaneous speed is too slow, we don't care the global average speed
                    trace!("headers-sync: the instantaneous speed is too slow");
                    Some(true)
```

**File:** sync/src/types/mod.rs (L241-253)
```rust
                        let spent_since_started = now.saturating_sub(self.started_ts);
                        let synced_since_started = now_tip_ts.saturating_sub(self.started_tip_ts);

                        let expected_since_started =
                            expected_headers_per_sec * spent_since_started * POW_INTERVAL / 1000;

                        if synced_since_started < expected_since_started {
                            // the global average speed is too slow
                            trace!(
                                "headers-sync: both the global average speed and the instantaneous speed \
                                are slower than expected"
                            );
                            Some(true)
```

**File:** sync/src/synchronizer/mod.rs (L451-466)
```rust
    fn better_tip_header(&self) -> HeaderIndexView {
        let (header, total_difficulty) = {
            let active_chain = self.shared.active_chain();
            (
                active_chain.tip_header(),
                active_chain.total_difficulty().to_owned(),
            )
        };
        let best_known = self.shared.state().shared_best_header();
        // is_better_chain
        if total_difficulty > *best_known.total_difficulty() {
            (header, total_difficulty).into()
        } else {
            best_known
        }
    }
```

**File:** sync/src/synchronizer/mod.rs (L549-571)
```rust
    pub fn eviction(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
        let active_chain = self.shared.active_chain();
        let mut eviction = Vec::new();
        let better_tip_header = self.better_tip_header();
        for mut kv_pair in self.peers().state.iter_mut() {
            let (peer, state) = kv_pair.pair_mut();
            let now = unix_time_as_millis();

            if let Some(ref mut controller) = state.headers_sync_controller {
                let better_tip_ts = better_tip_header.timestamp();
                if let Some(is_timeout) = controller.is_timeout(better_tip_ts, now) {
                    if is_timeout {
                        eviction.push(*peer);
                        continue;
                    }
                } else {
                    active_chain.send_getheaders_to_peer(
                        nc,
                        *peer,
                        better_tip_header.number_and_hash(),
                    );
                }
            }
```

**File:** verification/src/header_verifier.rs (L76-86)
```rust
        let min = self.data_loader.block_median_time(
            &self.header.data().raw().parent_hash(),
            self.median_block_count,
        );
        if self.header.timestamp() <= min {
            return Err(TimestampError::BlockTimeTooOld {
                min,
                actual: self.header.timestamp(),
            }
            .into());
        }
```
