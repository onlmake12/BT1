### Title
Non-Atomic `unverified_tip` State Overwrite Between Concurrent Chain Threads Causes Sync Scheduler Corruption - (File: chain/src/orphan_broker.rs)

---

### Summary

The `unverified_tip` shared state in `Shared` is written by two concurrent threads — `ChainService` and `ConsumeUnverifiedBlocks` — without any compare-and-swap coordination. The `send_unverified_block` function in `OrphanBroker` performs a non-atomic read-compare-write: it reads the **verified** tip from the snapshot, compares the incoming block number against it, and then unconditionally overwrites `unverified_tip` via a plain `ArcSwap::store`. This allows the `ChainService` thread to silently overwrite a corrective reset performed by `ConsumeUnverifiedBlocks` when a block fails verification, leaving `unverified_tip` pointing to a stale, too-high value. The sync scheduler then makes incorrect block-fetch and peer-disconnect decisions based on this corrupted state.

---

### Finding Description

**Shared state and its writers**

`Shared::unverified_tip` is declared as `Arc<ArcSwap<HeaderIndex>>`: [1](#0-0) 

It is written by two independent threads:

1. **`ChainService` thread** — via `OrphanBroker::send_unverified_block`, which advances `unverified_tip` upward as new blocks are queued for verification.
2. **`ConsumeUnverifiedBlocks` thread** — via `consume_unverified_blocks`, which resets `unverified_tip` back to the verified tip whenever a block fails contextual verification.

**The non-atomic check-then-set in `send_unverified_block`** [2](#0-1) 

The guard condition `block_number > self.shared.snapshot().tip_number()` reads the **verified** tip from the snapshot, not the current value of `unverified_tip`. The subsequent `set_unverified_tip` call is a plain `ArcSwap::store` with no compare-and-swap: [3](#0-2) 

**The concurrent reset in `ConsumeUnverifiedBlocks`**

When a block fails verification, the `ConsumeUnverifiedBlocks` thread resets `unverified_tip` to the current verified tip: [4](#0-3) 

**The race**

Because neither writer holds a lock across the read-compare-write sequence, the following interleaving is possible:

1. `ConsumeUnverifiedBlocks` fails block M (e.g., sent by a malicious peer), reads verified tip = 100, calls `set_unverified_tip(100)` — correct reset.
2. Concurrently, `ChainService` has already sent block N (N > M, e.g., 110) to the `PreloadUnverified` channel and is about to execute the guard check.
3. `ChainService` evaluates `110 > 100` (verified tip) → `true`.
4. `ChainService` calls `set_unverified_tip(110)`, **overwriting** the corrective reset from step 1.

Result: `unverified_tip = 110` even though block M (a prerequisite for block N) was invalid and the chain is stuck at verified tip 100.

A secondary form of the same bug occurs within the `ChainService` thread itself: blocks arriving out of order (e.g., 110, then 108, then 105) each compare against the verified tip (not the current `unverified_tip`), so the last-processed block sets `unverified_tip` to its number, potentially overwriting a higher value set by an earlier block.

---

### Impact Explanation

`unverified_tip` is consumed by the sync scheduler in two critical places:

**1. Inflight block pruning** (`sync/src/synchronizer/mod.rs`): [5](#0-4) 

`prune(unverified_tip)` removes blocks from the inflight set that are below `unverified_tip`, treating them as already being processed. If `unverified_tip` is incorrectly high, blocks that have not yet been received or that need to be re-fetched (because their predecessor was invalid) are silently dropped from the inflight set. The scheduler will not re-request them, causing a **sync stall**.

**2. Peer disconnection**: [6](#0-5) 

Peers whose `best_known` is below the (incorrectly high) `unverified_tip` are added to the disconnect list. This can disconnect honest peers that are still needed to supply the missing blocks, compounding the sync stall.

**3. Slow-block marking** (`sync/src/synchronizer/block_fetcher.rs`): [7](#0-6) 

An inflated `unverified_tip` causes `mark_slow_block` to fire incorrectly, penalizing honest peers.

The combined effect is a **node sync stall** that persists until the node reconnects to peers and re-discovers the missing blocks, which may take a significant amount of time.

---

### Likelihood Explanation

The race requires only that a malicious (or simply faulty) peer send one invalid block. This is trivially achievable by any unprivileged P2P peer — no keys, no hashpower, no Sybil attack required. The timing window between the `ConsumeUnverifiedBlocks` reset and the `ChainService` overwrite is narrow but real: both threads run concurrently and the `ArcSwap::store` is a single pointer swap with no mutual exclusion. Under normal IBD or steady-state sync with multiple peers, the node processes many blocks per second, making the race practically reachable.

---

### Recommendation

Replace the unconditional `ArcSwap::store` in `set_unverified_tip` with a compare-and-swap loop that only advances `unverified_tip` if the new value is strictly greater than the current value:

```rust
pub fn set_unverified_tip(&self, header: crate::HeaderIndex) {
    let new_number = header.number();
    loop {
        let current = self.unverified_tip.load();
        if current.number() >= new_number {
            break; // never go backwards
        }
        if self.unverified_tip.compare_and_swap(&current, Arc::new(header.clone())).is_ok() {
            break;
        }
    }
}
```

In `send_unverified_block`, change the guard to compare against the current `unverified_tip` (not the verified tip snapshot) to prevent out-of-order blocks from overwriting a higher value:

```rust
if block_number > self.shared.get_unverified_tip().number() {
    self.shared.set_unverified_tip(...);
}
```

In `consume_unverified_blocks`, the reset to verified tip should also use compare-and-swap so it only resets if `unverified_tip` is currently higher than the verified tip, preventing a stale `ChainService` write from re-inflating it.

---

### Proof of Concept

1. Node is syncing; verified tip = 100, `unverified_tip` = 100.
2. Malicious peer sends invalid block 105. `ChainService` stores it and queues it for verification.
3. Honest peer sends block 110. `ChainService` stores it, sends it to `PreloadUnverified`, and evaluates `110 > 100` → `true`.
4. **Race window opens**: `ConsumeUnverifiedBlocks` fails block 105, calls `set_unverified_tip(100)`.
5. `ChainService` calls `set_unverified_tip(110)`, overwriting the reset. `unverified_tip` = 110.
6. Sync scheduler runs: `prune(110)` removes inflight blocks 101–110 from the inflight set; peers with `best_known < 110` are disconnected.
7. Block 110 subsequently fails (its parent 105 was invalid). `ConsumeUnverifiedBlocks` resets `unverified_tip` = 100.
8. But the inflight blocks 101–109 have already been pruned and the peers that could supply them have been disconnected. The node stalls at verified tip 100 until it reconnects and re-discovers the missing blocks.

### Citations

**File:** shared/src/shared.rs (L73-73)
```rust
    pub(crate) unverified_tip: Arc<ArcSwap<crate::HeaderIndex>>,
```

**File:** shared/src/shared.rs (L407-409)
```rust
    pub fn set_unverified_tip(&self, header: crate::HeaderIndex) {
        self.unverified_tip.store(Arc::new(header));
    }
```

**File:** chain/src/orphan_broker.rs (L180-196)
```rust
        if block_number > self.shared.snapshot().tip_number() {
            self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                block_number,
                block_hash.clone(),
                U256::from(0u64),
            ));

            if let Some(handle) = ckb_metrics::handle() {
                handle.ckb_chain_unverified_tip.set(block_number as i64);
            }
            debug!(
                "set unverified_tip to {}-{}, while unverified_tip - verified_tip = {}",
                block_number.clone(),
                block_hash,
                block_number.saturating_sub(self.shared.snapshot().tip_number())
            )
        }
```

**File:** chain/src/verify.rs (L167-171)
```rust
                self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                    tip.number(),
                    tip.hash(),
                    tip_ext.total_difficulty,
                ));
```

**File:** sync/src/synchronizer/mod.rs (L748-755)
```rust
        let unverified_tip = self.shared.active_chain().unverified_tip_number();

        let disconnect_list = {
            let mut list = self
                .shared()
                .state()
                .write_inflight_blocks()
                .prune(unverified_tip);
```

**File:** sync/src/synchronizer/mod.rs (L759-764)
```rust
                list.extend(
                    self.shared
                        .state()
                        .peers()
                        .get_best_known_less_than_tip_and_unknown_empty(unverified_tip),
                )
```

**File:** sync/src/synchronizer/block_fetcher.rs (L305-313)
```rust
        let tip = self.active_chain.tip_number();
        let unverified_tip = self.active_chain.unverified_tip_number();
        let should_mark = fetch.last().is_some_and(|header| {
            header.number().saturating_sub(CHECK_POINT_WINDOW) > unverified_tip
        });
        if should_mark {
            state
                .write_inflight_blocks()
                .mark_slow_block(unverified_tip);
```
