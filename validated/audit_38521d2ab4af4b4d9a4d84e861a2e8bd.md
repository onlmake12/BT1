### Title
Orphan Pool Permanent Memory Leak via Far-Future Epoch in `need_clean` Single-Block Sampling — (`chain/src/utils/orphan_block_pool.rs`)

### Summary

`need_clean` samples only one arbitrary block per parent group (via `HashMap::iter().next()`) to decide whether the entire group is expired. An attacker can submit a structurally valid orphan block with a crafted far-future `epoch_number` that passes non-contextual verification (which does **not** check PoW or epoch continuity). That block's group is then permanently immune to the 60-second eviction timer, causing unbounded orphan pool growth.

---

### Finding Description

**`need_clean` samples only one block per parent group** [1](#0-0) 

`map.iter().next()` on a `HashMap<Byte32, LonelyBlockHash>` returns an **arbitrary** entry. If that entry's `epoch_number` is far in the future, the condition `epoch_number + EXPIRED_EPOCH < tip_epoch` is permanently false, and `remove_blocks_by_parent` is never called for that leader.

**`clean_expired_blocks` iterates all leaders and delegates to `need_clean`** [2](#0-1) 

A single "poisoned" leader blocks eviction of its entire descendant subtree.

**The 60-second timer calls `clean_expired_orphans`** [3](#0-2) 

There is no other eviction path; if `need_clean` returns false, the block stays forever.

**`non_contextual_verify` does NOT check PoW or epoch numbers** [4](#0-3) 

It calls only `BlockVerifier` and `NonContextualBlockTxsVerifier`.

**`BlockVerifier` checks only structural properties — no PoW, no epoch** [5](#0-4) 

It verifies: proposals limit, block bytes, cellbase structure, duplicate txs, and merkle root. Epoch number and PoW are absent.

**PoW and epoch continuity live in `HeaderVerifier`, which requires the parent** [6](#0-5) 

`HeaderVerifier` calls `PowVerifier` and `EpochVerifier`, but it requires `get_header_fields(&header.parent_hash())`. For an orphan block the parent is unknown, so this verifier is never invoked on the orphan path.

**Epoch number is taken directly from the block header field** [7](#0-6) 

`block.epoch().number()` is stored verbatim into `LonelyBlockHash::epoch_number` with no range check.

---

### Impact Explanation

An attacker submits one or more orphan blocks (parent hash unknown to the node), each with `epoch_number = u64::MAX - 1` (or any value ≥ `tip_epoch + EXPIRED_EPOCH + 1`). Each block:

1. Passes `non_contextual_verify` (no PoW, no epoch check).
2. Is inserted into the DB and the orphan pool.
3. Becomes a leader whose `need_clean` check permanently returns `false`.

The orphan pool has no enforced capacity cap: [8](#0-7) 

`with_capacity` only pre-allocates HashMap buckets; it does not enforce a maximum. Repeated submissions grow `blocks`, `parents`, and `leaders` without bound, consuming heap memory until the node OOMs or becomes unresponsive.

---

### Likelihood Explanation

- **Unprivileged**: any P2P peer can relay a block.
- **No PoW required**: `BlockVerifier` (the only non-contextual check applied) does not verify PoW. The attacker needs only a structurally valid block (valid cellbase, correct merkle root, valid tx structure).
- **Repeatable**: each unique parent hash creates a new permanent leader. A single attacker can flood the pool at the rate the node accepts relay messages.
- **No chain advancement needed**: the poisoned blocks are never evicted regardless of how many epochs the honest chain advances.

---

### Recommendation

1. **Fix `need_clean` to check ALL blocks in the group**, or use the minimum `epoch_number` across the group:
   ```rust
   fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
       self.blocks
           .get(parent_hash)
           .map(|map| {
               map.values().all(|b| b.epoch_number() + EXPIRED_EPOCH < tip_epoch)
           })
           .unwrap_or_default()
   }
   ```
2. **Enforce a hard cap on orphan pool size** and evict by insertion order (LRU or FIFO) when the cap is reached.
3. **Add PoW verification to the non-contextual block path** so structurally valid but PoW-invalid blocks are rejected before DB insertion.

---

### Proof of Concept

```
1. tip_epoch = 10 (node is at epoch 10)
2. Craft block B:
     parent_hash  = <random 32 bytes, not in chain>
     epoch        = EpochNumberWithFraction::new(10_000, 0, 1000)
                    → epoch_number() = 10_000
     compact_target = 0x207fffff  (minimum difficulty, any value)
     nonce        = 0             (no PoW needed; BlockVerifier won't check)
     transactions = [valid_cellbase]
     merkle_root  = hash(valid_cellbase)
3. Submit B via P2P block-relay message.
4. chain_service::asynchronous_process_block:
     non_contextual_verify(B) → Ok  (BlockVerifier passes)
     insert_block(B)          → Ok  (stored in DB)
     process_lonely_block(B)  → parent unknown → orphan_pool.insert(B)
5. Advance honest chain by 20 epochs (tip_epoch = 30).
6. 60-second timer fires → clean_expired_orphans():
     tip_epoch = 30
     need_clean(B.parent_hash, 30):
       epoch_number(B) + EXPIRED_EPOCH < tip_epoch
       10_000 + 6 < 30  → false
     → B is NOT evicted.
7. Repeat step 2-4 with different parent hashes → pool grows without bound.
``` [1](#0-0) [5](#0-4) [9](#0-8)

### Citations

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

**File:** chain/src/utils/orphan_block_pool.rs (L128-131)
```rust
#[derive(Default)]
pub struct OrphanBlockPool {
    inner: RwLock<InnerPool>,
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

**File:** chain/src/chain_service.rs (L117-143)
```rust
        if lonely_block.switch().is_none()
            || matches!(lonely_block.switch(), Some(switch) if !switch.disable_non_contextual())
        {
            let result = self.non_contextual_verify(lonely_block.block());
            if let Err(err) = result {
                error!(
                    "block {}-{} verify failed: {:?}",
                    block_number, block_hash, err
                );
                self.shared
                    .insert_block_status(lonely_block.block().hash(), BlockStatus::BLOCK_INVALID);
                lonely_block.execute_callback(Err(err));
                return;
            }
        }

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

**File:** verification/src/block_verifier.rs (L36-48)
```rust
impl<'a> Verifier for BlockVerifier<'a> {
    type Target = BlockView;

    fn verify(&self, target: &BlockView) -> Result<(), Error> {
        let max_block_proposals_limit = self.consensus.max_block_proposals_limit();
        let max_block_bytes = self.consensus.max_block_bytes();
        BlockProposalsLimitVerifier::new(max_block_proposals_limit).verify(target)?;
        BlockBytesVerifier::new(max_block_bytes).verify(target)?;
        CellbaseVerifier::new().verify(target)?;
        DuplicateVerifier::new().verify(target)?;
        MerkleRootVerifier::new().verify(target)
    }
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

**File:** chain/src/lib.rs (L97-98)
```rust
        let epoch_number: EpochNumber = block.epoch().number();

```
