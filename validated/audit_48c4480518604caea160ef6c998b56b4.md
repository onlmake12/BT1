### Title
Non-Deterministic Orphan Expiry via Single-Entry HashMap Sampling in `need_clean` — (`chain/src/utils/orphan_block_pool.rs`)

---

### Summary

`InnerPool::need_clean` samples only the first entry from a `HashMap<Byte32, LonelyBlockHash>` to decide whether all blocks under a given `parent_hash` should be evicted. Because Rust's `HashMap` iteration order is non-deterministic (randomized per-process seed), an unprivileged sync peer can craft two blocks sharing the same `parent_hash` but carrying heterogeneous `epoch_number` values — one expired, one not — and cause either premature eviction of a legitimate orphan block or indefinite on-disk retention of an expired one.

---

### Finding Description

**The defective function** is `need_clean` in `chain/src/utils/orphan_block_pool.rs`:

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .and_then(|map| {
            map.iter().next().map(|(_, lonely_block)| {   // ← arbitrary first entry
                lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
            })
        })
        .unwrap_or_default()
}
``` [1](#0-0) 

The inner structure is `HashMap<ParentHash, HashMap<packed::Byte32, LonelyBlockHash>>`. Multiple blocks can share the same `parent_hash` (competing blocks at the same height). `map.iter().next()` returns an arbitrary entry; if that entry's `epoch_number` is expired the entire sibling group is evicted via `remove_blocks_by_parent`, and if it is not expired the entire group is retained. [2](#0-1) 

**Why crafted blocks pass non-contextual verification:**

`asynchronous_process_block` calls `non_contextual_verify`, which runs only `BlockVerifier` and `NonContextualBlockTxsVerifier`: [3](#0-2) 

`BlockVerifier` checks cellbase, merkle roots, block bytes, proposals, and duplicates — it does **not** check PoW and does **not** check epoch continuity: [4](#0-3) 

PoW and epoch continuity are checked only in `HeaderVerifier`, which is contextual (requires the parent header from the chain store): [5](#0-4) 

Therefore a malicious peer can send a block with an arbitrary `epoch_number` (e.g., epoch 0, which is expired) and the same `parent_hash` as a legitimate orphan block. It will pass `non_contextual_verify`, be written to disk via `insert_block`, and be inserted into the orphan pool. [6](#0-5) 

**The timer fires every 60 seconds:** [7](#0-6) 

`clean_expired_orphans` fetches `tip_epoch_number` and calls `clean_expired_blocks(tip_epoch_number)`: [8](#0-7) 

`clean_expired_blocks` iterates leaders and calls `need_clean` per leader: [9](#0-8) 

---

### Impact Explanation

Two outcomes, both harmful:

| HashMap seed picks… | `need_clean` returns | Outcome |
|---|---|---|
| Crafted block (epoch 0, expired) | `true` | Legitimate orphan block evicted prematurely; sync stalls until re-requested |
| Legitimate block (epoch N, not expired) | `false` | Crafted block (and its on-disk record) retained indefinitely; storage bloat |

In the premature-eviction case, `delete_unverified_block` removes the legitimate block from the store and `remove_header_view`/`remove_block_status` strip its metadata: [10](#0-9) 

In the indefinite-retention case, the expired block's raw data remains on disk and its `block_status` entry persists in memory, consuming both storage and the in-memory status map indefinitely.

---

### Likelihood Explanation

- The attacker is an unprivileged P2P sync peer; no key or privilege is required.
- Crafting a block with an arbitrary `epoch_number` and a target `parent_hash` requires only constructing a valid `BlockBuilder` payload — no PoW is needed because `BlockVerifier` does not check it.
- The attack is repeatable: the peer can flood the node with many such crafted blocks under many different `parent_hash` values, amplifying both the eviction and retention effects.
- The 60-second cleanup timer provides a reliable trigger window.

---

### Recommendation

Replace the single-entry sample with a check over **all** entries under the parent. The correct invariant is: evict only when **every** sibling block is expired (use `all`), or alternatively store the minimum epoch at insertion time.

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .map(|map| {
            !map.is_empty()
                && map.values().all(|b| b.epoch_number() + EXPIRED_EPOCH < tip_epoch)
        })
        .unwrap_or_default()
}
```

---

### Proof of Concept

```rust
// Insert two LonelyBlockHash entries under the same parent_hash:
//   block_A: epoch_number = 1  (expired when tip_epoch = 20, EXPIRED_EPOCH = 6)
//   block_B: epoch_number = 15 (NOT expired when tip_epoch = 20)
//
// Both share parent_hash = P.
//
// Call pool.clean_expired_blocks(20) repeatedly across processes with
// different HashMap seeds (RUSTFLAGS="-C target-cpu=native" or use
// std::collections::HashMap with explicit random seeds via ahash).
//
// Assertion: outcome is non-deterministic.
//   - Some runs: both blocks evicted (block_B prematurely removed)
//   - Other runs: neither block evicted (block_A retained indefinitely)
//
// Attacker replication: send block_A via P2P SendBlock message with
//   parent_hash = hash of a known orphan's parent,
//   epoch = EpochNumberWithFraction::new(1, 0, 1000),  // epoch 1
//   compact_target = 0x207fffff,  // minimum difficulty, no PoW needed
//   valid merkle root, valid cellbase.
// Block passes non_contextual_verify and is inserted into orphan pool.
``` [11](#0-10)

### Citations

**File:** chain/src/utils/orphan_block_pool.rs (L16-18)
```rust
struct InnerPool {
    // Group by blocks in the pool by the parent hash.
    blocks: HashMap<ParentHash, HashMap<packed::Byte32, LonelyBlockHash>>,
```

**File:** chain/src/utils/orphan_block_pool.rs (L98-110)
```rust
    /// cleanup expired blocks(epoch + EXPIRED_EPOCH < tip_epoch)
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

**File:** chain/src/utils/orphan_block_pool.rs (L112-122)
```rust
    /// get 1st block belongs to that parent and check if it's expired block
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

**File:** chain/src/chain_service.rs (L133-143)
```rust
        if let Err(err) = self.insert_block(&lonely_block) {
            error!(
                "insert block {}-{} failed: {:?}",
                block_number, block_hash, err
            );
            self.shared.block_status_map().remove(&block_hash);
            lonely_block.execute_callback(Err(err));
            return;
        }

        self.orphan_broker.process_lonely_block(lonely_block.into());
```

**File:** verification/src/block_verifier.rs (L15-27)
```rust
/// Block verifier that are independent of context.
///
/// Contains:
/// - [`CellbaseVerifier`](./struct.CellbaseVerifier.html)
/// - [`BlockBytesVerifier`](./struct.BlockBytesVerifier.html)
/// - [`BlockExtensionVerifier`](./struct.BlockExtensionVerifier.html)
/// - [`BlockProposalsLimitVerifier`](./struct.BlockProposalsLimitVerifier.html)
/// - [`DuplicateVerifier`](./struct.DuplicateVerifier.html)
/// - [`MerkleRootVerifier`](./struct.MerkleRootVerifier.html)
#[derive(Clone)]
pub struct BlockVerifier<'a> {
    consensus: &'a Consensus,
}
```

**File:** verification/src/header_verifier.rs (L30-50)
```rust
impl<'a, DL: HeaderFieldsProvider> Verifier for HeaderVerifier<'a, DL> {
    type Target = HeaderView;
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
        let parent_fields = self
            .data_loader
            .get_header_fields(&header.parent_hash())
            .ok_or_else(|| UnknownParentError {
                parent_hash: header.parent_hash(),
            })?;
        NumberVerifier::new(parent_fields.number, header).verify()?;
        EpochVerifier::new(parent_fields.epoch, header).verify()?;
        TimestampVerifier::new(
            self.data_loader,
            header,
            self.consensus.median_time_block_count(),
        )
        .verify()?;
        Ok(())
    }
```

**File:** chain/src/orphan_broker.rs (L134-156)
```rust
    pub(crate) fn clean_expired_orphans(&self) {
        debug!("clean expired orphans");
        let tip_epoch_number = self
            .shared
            .store()
            .get_tip_header()
            .expect("tip header")
            .epoch()
            .number();
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
    }
```
