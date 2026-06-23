### Title
`send_unverified_block()` Compares Against Verified Tip Instead of Unverified Tip, Allowing `unverified_tip` High-Water Mark to Decrease — (`chain/src/orphan_broker.rs`)

---

### Summary

In `chain/src/orphan_broker.rs`, the `send_unverified_block()` function updates the `unverified_tip` high-water mark using a condition that compares the incoming block number against the **verified** chain tip (`snapshot().tip_number()`) rather than the **current unverified tip** (`get_unverified_tip().number()`). This allows the `unverified_tip` to be unconditionally overwritten with a lower value, mirroring the Popcorn `syncFeeCheckpoint` bug exactly: a value that must only ever increase is instead set to any value above a lower baseline.

---

### Finding Description

The `unverified_tip` is a shared atomic high-water mark that tracks the highest block number that has been queued for asynchronous verification. It is intended to be monotonically non-decreasing during normal sync operation.

The defective update logic is in `send_unverified_block()`:

```rust
// chain/src/orphan_broker.rs, lines 180–185
if block_number > self.shared.snapshot().tip_number() {   // ← compares against VERIFIED tip
    self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
        block_number,
        block_hash.clone(),
        U256::from(0u64),
    ));
    ...
}
``` [1](#0-0) 

`set_unverified_tip` is an unconditional atomic store with no monotonicity guard:

```rust
// shared/src/shared.rs, lines 407–409
pub fn set_unverified_tip(&self, header: crate::HeaderIndex) {
    self.unverified_tip.store(Arc::new(header));
}
``` [2](#0-1) 

**Scenario:**
- Verified tip = 100; `unverified_tip` = 500 (blocks 101–500 are queued for verification).
- A sync peer delivers a valid block at height 200 whose parent (199) is already stored (`BLOCK_STORED` or `is_pending_verify`).
- `process_descendant(block_200)` → `send_unverified_block(block_200)` is called.
- Condition: `200 > 100` (verified tip) → **true**.
- `set_unverified_tip(200)` is called, **decreasing** the high-water mark from 500 to 200.

The correct condition must compare against the current `unverified_tip`, not the verified tip:

```rust
if block_number > self.shared.get_unverified_tip().number() {
    self.shared.set_unverified_tip(...);
}
```

---

### Impact Explanation

The `unverified_tip` value is consumed in three security-relevant paths inside the synchronizer:

**1. Block-download window gate** (`sync/src/synchronizer/block_fetcher.rs`, line 122):

```rust
if self.sync_shared.shared().get_unverified_tip().number() >= unverified_tip_limit {
    return None;   // stop fetching
}
``` [3](#0-2) 

`unverified_tip_limit = verified_tip + BLOCK_DOWNLOAD_WINDOW * 9`. Decreasing `unverified_tip` from 500 to 200 makes this guard pass again, causing the node to re-open the download window and issue redundant block requests for blocks it already has queued, wasting bandwidth and inflating inflight state.

**2. IBD peer-skipping logic** (`sync/src/synchronizer/block_fetcher.rs`, line 191–200):

```rust
if matches!(self.ibd, IBDState::In)
    && best_known.number() <= self.active_chain.unverified_tip_number()
{
    return None;   // skip this peer
}
``` [4](#0-3) 

With `unverified_tip` decreased to 200, peers whose `best_known` is between 201 and 500 are no longer skipped. The node re-requests blocks from those peers, causing duplicate inflight entries and degraded IBD throughput.

**3. Inflight-block pruning and slow-block marking** (`sync/src/synchronizer/mod.rs`, line 755; `block_fetcher.rs`, lines 307–313):

```rust
let unverified_tip = self.shared.active_chain().unverified_tip_number();
let disconnect_list = {
    let mut list = self.shared().state().write_inflight_blocks().prune(unverified_tip);
    ...
};
``` [5](#0-4) 

```rust
let should_mark = fetch.last().is_some_and(|header| {
    header.number().saturating_sub(CHECK_POINT_WINDOW) > unverified_tip
});
if should_mark {
    state.write_inflight_blocks().mark_slow_block(unverified_tip);
}
``` [6](#0-5) 

A decreased `unverified_tip` causes `mark_slow_block` to be called with a lower tip value, marking more inflight blocks as slow and triggering the penalty/re-request mechanism for blocks that are not actually slow. This degrades peer scoring and can cause unnecessary peer disconnections.

---

### Likelihood Explanation

The trigger requires an unprivileged sync peer to deliver a **valid** block whose parent is already stored, at a height lower than the current `unverified_tip` but above the verified tip. During IBD this is routine: the node downloads blocks from multiple peers concurrently and blocks arrive out of order. Any peer that delivers a lower-numbered block after a higher-numbered block has already been queued will trigger the bug without any special capability. No PoW mining is required — the peer simply relays existing chain blocks in a specific order. The condition is easily satisfied in normal multi-peer IBD operation, making accidental triggering likely and deliberate triggering trivial.

---

### Recommendation

Change the guard in `send_unverified_block` to compare against the current `unverified_tip` instead of the verified snapshot tip:

```rust
// chain/src/orphan_broker.rs
fn send_unverified_block(&self, lonely_block: LonelyBlockHash) {
    let block_number = lonely_block.block_number_and_hash.number();
    let block_hash  = lonely_block.block_number_and_hash.hash();
    // ... send to channel ...
    // Only advance the high-water mark; never decrease it.
    if block_number > self.shared.get_unverified_tip().number() {
        self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
            block_number,
            block_hash.clone(),
            U256::from(0u64),
        ));
    }
}
```

This mirrors the corrected Popcorn pattern: `if (shareValue > highWaterMark) highWaterMark = shareValue`.

---

### Proof of Concept

**State before attack:**
- Verified tip = block 100 (`snapshot().tip_number() == 100`)
- `unverified_tip` = block 500 (blocks 101–500 queued for async verification)

**Attacker action:**
- Sync peer sends block 200 (valid PoW, parent = block 199 which is `BLOCK_STORED`).

**Code path triggered:**
1. `BlockProcess::execute(block_200)` → `asynchronous_process_remote_block(block_200)`
2. `OrphanBroker::process_lonely_block(block_200)`: parent 199 is `BLOCK_STORED` → calls `process_descendant(block_200)`
3. `process_descendant` → `send_unverified_block(block_200)`
4. Block sent to preload channel successfully.
5. Condition: `200 > self.shared.snapshot().tip_number()` → `200 > 100` → **true**
6. `set_unverified_tip(200)` executes — `unverified_tip` drops from **500 → 200**.

**Downstream effect (next `find_blocks_to_fetch` poll):**
- `unverified_tip_limit = 100 + BLOCK_DOWNLOAD_WINDOW * 9`; `200 < unverified_tip_limit` → download gate re-opens.
- IBD peer-skip check: peers with `best_known` in [201, 500] are no longer skipped → redundant block requests issued.
- `mark_slow_block(200)` fires for blocks that are not actually slow, degrading peer scoring. [7](#0-6) [8](#0-7) [9](#0-8) [4](#0-3) [5](#0-4)

### Citations

**File:** chain/src/orphan_broker.rs (L158-197)
```rust
    fn send_unverified_block(&self, lonely_block: LonelyBlockHash) {
        let block_number = lonely_block.block_number_and_hash.number();
        let block_hash = lonely_block.block_number_and_hash.hash();

        if let Some(metrics) = ckb_metrics::handle() {
            metrics
                .ckb_chain_preload_unverified_block_ch_len
                .set(self.preload_unverified_tx.len() as i64)
        }

        match self.preload_unverified_tx.send(lonely_block) {
            Ok(_) => {
                debug!(
                    "process desendant block success {}-{}",
                    block_number, block_hash
                );
            }
            Err(_) => {
                info!("send unverified_block_tx failed, the receiver has been closed");
                return;
            }
        };
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
    }
```

**File:** shared/src/shared.rs (L407-412)
```rust
    pub fn set_unverified_tip(&self, header: crate::HeaderIndex) {
        self.unverified_tip.store(Arc::new(header));
    }
    pub fn get_unverified_tip(&self) -> crate::HeaderIndex {
        self.unverified_tip.load().as_ref().clone()
    }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L111-129)
```rust
        let Some(unverified_tip_limit) = self
            .sync_shared
            .active_chain()
            .tip_number()
            .checked_add(BLOCK_DOWNLOAD_WINDOW * 9)
        else {
            trace!(
                "active chain tip is too close to BlockNumber::MAX to calculate unverified tip limit"
            );
            return None;
        };
        if self.sync_shared.shared().get_unverified_tip().number() >= unverified_tip_limit {
            trace!(
                "unverified_tip - tip > BLOCK_DOWNLOAD_WINDOW * 9, skip fetch, unverified_tip: {}, tip: {}",
                self.sync_shared.shared().get_unverified_tip().number(),
                self.sync_shared.active_chain().tip_number()
            );
            return None;
        }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L191-200)
```rust
        if matches!(self.ibd, IBDState::In)
            && best_known.number() <= self.active_chain.unverified_tip_number()
        {
            debug!(
                "In IBD mode, Peer {}'s best_known: {} is less or equal than unverified_tip : {}, won't request block from this peer",
                self.peer,
                best_known.number(),
                self.active_chain.unverified_tip_number()
            );
            return None;
```

**File:** sync/src/synchronizer/block_fetcher.rs (L307-314)
```rust
        let should_mark = fetch.last().is_some_and(|header| {
            header.number().saturating_sub(CHECK_POINT_WINDOW) > unverified_tip
        });
        if should_mark {
            state
                .write_inflight_blocks()
                .mark_slow_block(unverified_tip);
        }
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
