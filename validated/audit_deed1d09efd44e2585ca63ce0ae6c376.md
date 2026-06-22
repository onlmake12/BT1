The code is now fully traced. Let me confirm the two critical facts before rendering the verdict.

**Fact 1 — `need_clean` only samples one child:** [1](#0-0) 

`map.iter().next()` returns one arbitrary entry from the inner `HashMap<Byte32, LonelyBlockHash>`. If that entry has a recent epoch, the function returns `false` and the entire sibling group — including expired-epoch blocks — is skipped.

**Fact 2 — PoW and epoch are NOT checked before orphan pool insertion:**

`non_contextual_verify` in `chain_service.rs` calls only `BlockVerifier` and `NonContextualBlockTxsVerifier`. [2](#0-1) 

`BlockVerifier::verify` runs `BlockProposalsLimitVerifier`, `BlockBytesVerifier`, `CellbaseVerifier`, `DuplicateVerifier`, and `MerkleRootVerifier` — **no PoW, no epoch check**. [3](#0-2) 

PoW and epoch validation live in `HeaderVerifier`, which requires the parent block (contextual) and is **never called** on this path. [4](#0-3) 

After passing `non_contextual_verify`, the block is stored to DB and handed to `orphan_broker.process_lonely_block` → `orphan_blocks_broker.insert` with no further epoch gating. [5](#0-4) 

---

### Title
`InnerPool::need_clean` samples only the first HashMap child, allowing an attacker to permanently suppress orphan-pool cleanup — (`chain/src/utils/orphan_block_pool.rs`)

### Summary

`need_clean` decides whether to evict a parent group by inspecting exactly one child block chosen by non-deterministic `HashMap::iter().next()`. Because PoW and epoch numbers are not validated before orphan-pool insertion, an unprivileged P2P peer can insert a "shield" block with a recent epoch alongside expired-epoch siblings under the same fake parent hash. Whenever the shield block is iterated first, `need_clean` returns `false` and the expired siblings are never evicted, causing unbounded memory growth in the orphan pool.

### Finding Description

`InnerPool::clean_expired_blocks` iterates over all leader hashes and calls `need_clean` for each:

```rust
// chain/src/utils/orphan_block_pool.rs  lines 99-122
pub fn clean_expired_blocks(&mut self, tip_epoch: EpochNumber) -> Vec<LonelyBlockHash> {
    for hash in self.leaders.clone().iter() {
        if self.need_clean(hash, tip_epoch) { ... }
    }
}

fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .and_then(|map| {
            map.iter().next().map(|(_, lonely_block)| {   // ← only first entry
                lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
            })
        })
        .unwrap_or_default()
}
```

`map` is a `HashMap<Byte32, LonelyBlockHash>` whose iteration order is randomised per-process. If a parent has two children — one with epoch 1 (expired, tip=20, EXPIRED_EPOCH=6) and one with epoch 19 (recent) — and the recent child is returned by `.next()`, `need_clean` returns `false` and neither child is ever cleaned.

The precondition is trivially reachable because `non_contextual_verify` (`BlockVerifier` + `NonContextualBlockTxsVerifier`) does **not** check PoW or epoch numbers. Any P2P peer can send a `SendBlock` message containing a block with an arbitrary epoch field; it will pass `non_contextual_verify`, be written to the DB, and be inserted into the orphan pool. [6](#0-5) [7](#0-6) 

### Impact Explanation

The orphan block pool has no hard eviction cap (unlike the tx-orphan pool which enforces `DEFAULT_MAX_ORPHAN_TRANSACTIONS`). The only reclamation path is `clean_expired_blocks`, fired every 60 seconds. [8](#0-7) 

With the shield technique, ~50% of inserted expired groups survive each cleanup pass (those where the shield block happens to be iterated first). An attacker sending a steady stream of (expired, shield) pairs under distinct fake parent hashes causes monotonically growing RSS, degrading node performance and eventually causing OOM — matching the "network congestion / node degradation" scope.

### Likelihood Explanation

- No PoW required; any peer can send arbitrary `SendBlock` messages.
- No privileged role needed.
- The attack is cheap: two small crafted blocks per fake parent hash.
- The 60-second cleanup timer means the attacker needs only a modest send rate to outpace cleanup.
- The non-determinism is in the attacker's favour: on average half of all inserted groups escape cleanup permanently.

### Recommendation

Replace the single-sample check with a check over **all** children, using the minimum epoch:

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .map(|map| {
            map.values().any(|b| b.epoch_number() + EXPIRED_EPOCH < tip_epoch)
        })
        .unwrap_or_default()
}
```

Additionally, add PoW verification (via `HeaderVerifier`'s `PowVerifier`) to `non_contextual_verify` so that epoch-spoofed blocks are rejected at the network boundary before reaching the orphan pool.

### Proof of Concept

```rust
// Pseudocode — insert two children under the same fake parent,
// one expired, one recent; assert the expired one is cleaned.
let pool = OrphanBlockPool::with_capacity(10);
let fake_parent = random_byte32();

// expired: epoch 1, tip will be 20
let expired = make_lonely_block(fake_parent.clone(), epoch = 1);
// shield: epoch 19 (within EXPIRED_EPOCH=6 of tip=20)
let shield  = make_lonely_block(fake_parent.clone(), epoch = 19);

pool.insert(expired);
pool.insert(shield);

let removed = pool.clean_expired_blocks(20);

// BUG: if shield is iterated first by HashMap, removed.len() == 0
// The expired block is never cleaned.
assert_eq!(removed.len(), 1);  // fails ~50% of the time
```

### Citations

**File:** chain/src/utils/orphan_block_pool.rs (L98-122)
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
