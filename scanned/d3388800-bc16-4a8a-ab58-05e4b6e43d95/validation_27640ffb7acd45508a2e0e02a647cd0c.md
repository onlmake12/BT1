### Title
Incorrect Single-Child Epoch Sampling in `need_clean` Causes Non-Expired Orphan Blocks to Be Incorrectly Purged — (`chain/src/utils/orphan_block_pool.rs`)

---

### Summary

`InnerPool::need_clean` decides whether to purge an entire orphan subtree by inspecting only the **first** child returned by a non-deterministic `HashMap` iterator. An unprivileged P2P peer can insert two orphan blocks sharing the same parent hash but carrying different epoch numbers (both passing non-contextual verification). When the expired child happens to be sampled first, `remove_blocks_by_parent` recursively deletes the **entire** subtree — including non-expired blocks — from both the in-memory pool and the on-disk database, permanently disrupting the node's sync state.

---

### Finding Description

`clean_expired_blocks` iterates over a cloned snapshot of `self.leaders` and, for each leader, calls `need_clean` to decide whether to purge:

```rust
// chain/src/utils/orphan_block_pool.rs  lines 99-110
pub fn clean_expired_blocks(&mut self, tip_epoch: EpochNumber) -> Vec<LonelyBlockHash> {
    let mut result = vec![];
    for hash in self.leaders.clone().iter() {
        if self.need_clean(hash, tip_epoch) {
            let descendants = self.remove_blocks_by_parent(hash);
            result.extend(descendants);
        }
    }
    result
}
``` [1](#0-0) 

`need_clean` samples **one arbitrary child** via `map.iter().next()` on a `HashMap`, whose iteration order is non-deterministic:

```rust
// lines 113-122
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .and_then(|map| {
            map.iter().next().map(|(_, lonely_block)| {
                lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
            })
        })
        .unwrap_or_default()
}
``` [2](#0-1) 

If the sampled child is expired, `remove_blocks_by_parent` is called, which recursively removes **all** children of that leader from `self.blocks`, `self.parents`, and `self.leaders`: [3](#0-2) 

The caller `clean_expired_orphans` then permanently deletes every returned block from the database and removes its `block_status` and `header_view`:

```rust
// chain/src/orphan_broker.rs  lines 134-155
for expired_orphan in expired_orphans {
    self.delete_block(&expired_orphan);
    self.shared.remove_header_view(&expired_orphan.hash());
    self.shared.remove_block_status(&expired_orphan.hash());
}
``` [4](#0-3) 

`clean_expired_orphans` is triggered every 60 seconds from the `ChainService` event loop: [5](#0-4) 

**Root cause**: `need_clean` assumes all children of a leader share the same epoch. This assumption is violated when an attacker submits two orphan blocks with the same parent hash but different epoch numbers. The non-contextual verifier only checks `is_well_formed()` on the epoch field — it does not validate that the epoch matches the actual chain epoch (that is a contextual check requiring the parent block): [6](#0-5) 

The non-contextual `EpochVerifier` in `verification/src/header_verifier.rs` only rejects malformed epochs (zero denominator, numerator ≥ denominator), not epochs that are inconsistent with the chain: [7](#0-6) 

---

### Impact Explanation

When the expired child is sampled first by `map.iter().next()`:

1. `need_clean` returns `true`.
2. `remove_blocks_by_parent` purges the **entire** subtree under the leader — including non-expired blocks crafted by the attacker.
3. `clean_expired_orphans` calls `delete_block` on every purged block, permanently removing their data from the RocksDB store.
4. `remove_block_status` is called on non-expired blocks, erasing their tracking state.
5. When the parent block eventually arrives, the node cannot process the non-expired orphans (they are gone from both pool and DB) and must re-request them from peers, causing sync delays and disruption.

Conversely, when the non-expired child is sampled first, `need_clean` returns `false` and the expired blocks are never cleaned, causing unbounded memory growth (DoS via orphan pool bloat).

---

### Likelihood Explanation

- Any unprivileged P2P peer can send blocks to the node.
- Crafting two blocks with the same parent hash but different well-formed epoch numbers is trivial (e.g., `EpochNumberWithFraction::new(3, 0, 1000)` and `EpochNumberWithFraction::new(10, 0, 1000)`).
- Both blocks pass `non_contextual_verify` and are inserted into the orphan pool under the same leader.
- The attacker only needs to observe which block hashes the target node is requesting (visible from the sync protocol) to know a suitable parent hash.
- `clean_expired_orphans` fires every 60 seconds, giving the attacker a reliable trigger window.

---

### Recommendation

`need_clean` must inspect **all** children of a leader, not just the first one. The subtree should only be purged if every direct child is expired:

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .map(|map| {
            !map.is_empty()
                && map.values().all(|lonely_block| {
                    lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
                })
        })
        .unwrap_or_default()
}
```

---

### Proof of Concept

```
Setup:
  tip_epoch = 10, EXPIRED_EPOCH = 6

Attacker sends to target node:
  B1: parent_hash = P, epoch = 3  (expired: 3 + 6 = 9 < 10)
  B2: parent_hash = P, epoch = 10 (not expired: 10 + 6 = 16 >= 10)

Both pass non_contextual_verify (epochs are well-formed).
Both are inserted into orphan pool: blocks[P] = {B1_hash: B1, B2_hash: B2}
Leader P is added to self.leaders.

60 seconds later, clean_expired_orphans fires:
  tip_epoch_number = 10
  clean_expired_blocks(10) iterates leaders, calls need_clean(P, 10)
  map.iter().next() → returns B1 (non-deterministic, 50% chance)
  B1.epoch_number() + EXPIRED_EPOCH = 9 < 10 → need_clean returns true
  remove_blocks_by_parent(P) removes BOTH B1 and B2 from pool and DB

Result:
  B2 (non-expired) is permanently deleted from DB and its block_status removed.
  When P later arrives, the node cannot process B2 and must re-request it from peers.
  Sync is disrupted.
```

### Citations

**File:** chain/src/utils/orphan_block_pool.rs (L56-88)
```rust
    pub fn remove_blocks_by_parent(&mut self, parent_hash: &ParentHash) -> Vec<LonelyBlockHash> {
        // try remove leaders first
        if !self.leaders.remove(parent_hash) {
            return Vec::new();
        }

        let mut queue: VecDeque<packed::Byte32> = VecDeque::new();
        queue.push_back(parent_hash.to_owned());

        let mut removed: Vec<LonelyBlockHash> = Vec::new();
        while let Some(parent_hash) = queue.pop_front() {
            if let Some(orphaned) = self.blocks.remove(&parent_hash) {
                let (hashes, blocks): (Vec<_>, Vec<_>) = orphaned.into_iter().unzip();
                for hash in hashes.iter() {
                    self.parents.remove(hash);
                }
                queue.extend(hashes);
                removed.extend(blocks);
            }
        }

        debug!("orphan pool pop chain len: {}", removed.len());
        debug_assert_ne!(
            removed.len(),
            0,
            "orphan pool removed list must not be zero"
        );

        shrink_to_fit!(self.blocks, SHRINK_THRESHOLD);
        shrink_to_fit!(self.parents, SHRINK_THRESHOLD);
        shrink_to_fit!(self.leaders, SHRINK_THRESHOLD);
        removed
    }
```

**File:** chain/src/utils/orphan_block_pool.rs (L99-110)
```rust
    pub fn clean_expired_blocks(&mut self, tip_epoch: EpochNumber) -> Vec<LonelyBlockHash> {
        let mut result = vec![];

        for hash in self.leaders.clone().iter() {
            if self.need_clean(hash, tip_epoch) {
                // remove items in orphan pool and return hash to callee(clean header map)
                let descendants = self.remove_blocks_by_parent(hash);
                result.extend(descendants);
            }
        }
        result
    }
```

**File:** chain/src/utils/orphan_block_pool.rs (L113-122)
```rust
    fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
        self.blocks
            .get(parent_hash)
            .and_then(|map| {
                map.iter().next().map(|(_, lonely_block)| {
                    lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
                })
            })
            .unwrap_or_default()
    }
```

**File:** chain/src/orphan_broker.rs (L143-155)
```rust
        let expired_orphans = self
            .orphan_blocks_broker
            .clean_expired_blocks(tip_epoch_number);
        for expired_orphan in expired_orphans {
            self.delete_block(&expired_orphan);
            self.shared.remove_header_view(&expired_orphan.hash());
            self.shared.remove_block_status(&expired_orphan.hash());
            info!(
                "cleaned expired orphan: {}-{}",
                expired_orphan.number(),
                expired_orphan.hash()
            );
        }
```

**File:** chain/src/chain_service.rs (L40-63)
```rust
        let clean_expired_orphan_timer =
            crossbeam::channel::tick(std::time::Duration::from_secs(60));

        loop {
            select! {
                recv(self.process_block_rx) -> msg => match msg {
                    Ok(Request { responder, arguments: lonely_block }) => {
                        // asynchronous_process_block doesn't interact with tx-pool,
                        // no need to pause tx-pool's chunk_process here.
                        let _trace_now = minstant::Instant::now();
                        self.asynchronous_process_block(lonely_block);
                        if let Some(handle) = ckb_metrics::handle(){
                            handle.ckb_chain_async_process_block_duration.observe(_trace_now.elapsed().as_secs_f64())
                        }
                        let _ = responder.send(());
                    },
                    _ => {
                        error!("process_block_receiver closed");
                        break;
                    },
                },
                recv(clean_expired_orphan_timer) -> _ => {
                    self.orphan_broker.clean_expired_orphans();
                },
```

**File:** chain/src/chain_service.rs (L72-89)
```rust
    fn non_contextual_verify(&self, block: &BlockView) -> Result<(), Error> {
        let consensus = self.shared.consensus();
        BlockVerifier::new(consensus).verify(block).map_err(|e| {
            debug!("[process_block] BlockVerifier error {:?}", e);
            e
        })?;

        NonContextualBlockTxsVerifier::new(consensus)
            .verify(block)
            .map_err(|e| {
                debug!(
                    "[process_block] NonContextualBlockTxsVerifier error {:?}",
                    e
                );
                e
            })
            .map(|_| ())
    }
```

**File:** verification/src/header_verifier.rs (L133-148)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        if !self.header.epoch().is_well_formed() {
            return Err(EpochError::Malformed {
                value: self.header.epoch(),
            }
            .into());
        }
        if !self.parent.is_genesis() && !self.header.epoch().is_successor_of(self.parent) {
            return Err(EpochError::NonContinuous {
                current: self.header.epoch(),
                parent: self.parent,
            }
            .into());
        }
        Ok(())
    }
```
