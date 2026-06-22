The code is now fully read. Let me analyze the exact logic.

**`need_clean` — confirmed single-sample logic:** [1](#0-0) 

It calls `map.iter().next()` on the inner `HashMap<packed::Byte32, LonelyBlockHash>` — a single arbitrary entry from an unordered map. If that entry has a far-future epoch, the entire subtree is skipped.

**`BlockVerifier` (non-contextual) — confirmed: no PoW check, no epoch check:** [2](#0-1) 

Only runs: `BlockProposalsLimitVerifier`, `BlockBytesVerifier`, `CellbaseVerifier`, `DuplicateVerifier`, `MerkleRootVerifier`. No `PowVerifier`, no `EpochVerifier`.

**`non_contextual_verify` — confirmed: only `BlockVerifier` + `NonContextualBlockTxsVerifier`:** [3](#0-2) 

**`HeaderVerifier` (contextual) — PoW and epoch are contextual only:** [4](#0-3) 

`PowVerifier` and `EpochVerifier` are only invoked in `HeaderVerifier`, which requires the parent block and is NOT called before orphan pool insertion.

**Orphan pool insertion path:** [5](#0-4) 

After passing `non_contextual_verify` (no PoW, no epoch), the block is stored to DB and forwarded to `orphan_broker.process_lonely_block`. [6](#0-5) 

If the parent is not stored, the block is inserted into the orphan pool with no further epoch validation.

---

### Title
`need_clean` samples a single arbitrary HashMap entry to decide subtree expiry, allowing an attacker to suppress orphan pool cleanup via a far-future-epoch block — (`chain/src/utils/orphan_block_pool.rs`)

### Summary

`InnerPool::need_clean` uses `map.iter().next()` on an unordered `HashMap` to sample exactly one direct child of a leader and uses that single child's epoch to decide whether the entire subtree should be evicted. Because non-contextual verification performs no PoW check and no epoch check, an unprivileged attacker can insert a block with an arbitrarily large epoch number into the orphan pool. By inserting enough such blocks as siblings of expired orphans under the same leader, the attacker can make it statistically likely (and with enough insertions, near-certain) that `need_clean` samples a far-future-epoch block and returns `false`, permanently suppressing cleanup of the expired subtree.

### Finding Description

**Root cause 1 — `need_clean` samples one entry:**

```rust
// chain/src/utils/orphan_block_pool.rs:117
map.iter().next().map(|(_, lonely_block)| {
    lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
})
```

`map` is `HashMap<packed::Byte32, LonelyBlockHash>`. `iter().next()` returns an arbitrary entry. If a leader has `N` children and `M` of them carry a far-future epoch, the probability that cleanup is suppressed is `M/N`. The attacker controls `M` by inserting more blocks. [1](#0-0) 

**Root cause 2 — no PoW or epoch check before orphan pool insertion:**

`BlockVerifier::verify` runs only structural checks: [2](#0-1) 

`PowVerifier` and `EpochVerifier` live exclusively in `HeaderVerifier`, which is contextual and requires the parent block: [4](#0-3) 

Neither is invoked in `non_contextual_verify`: [3](#0-2) 

Therefore a block with `epoch_number = tip_epoch + 10000` and `compact_target = 0x207fffff` (minimum difficulty) passes all pre-orphan-pool checks. The attacker mines it trivially (seconds of CPU time) and submits it via the standard P2P block relay path.

**Attack flow:**

1. Attacker observes a leader hash `L` (the parent hash of any orphan block visible on the network, or one they created themselves by sending a block whose parent is unknown).
2. Attacker inserts `K` expired-epoch blocks (epoch ≪ tip_epoch − 6) as children of `L`.
3. Attacker inserts `K` far-future-epoch blocks (epoch = tip_epoch + 10000, compact_target = min) as children of `L`. Each requires only trivial PoW.
4. Every 60 s, `clean_expired_orphan_timer` fires → `clean_expired_orphans` → `clean_expired_blocks` → `need_clean(L, tip_epoch)`.
5. With probability ≈ K/(2K) = 50% per cycle, a far-future block is sampled first → `need_clean` returns `false` → subtree not cleaned.
6. Attacker repeats step 3 to keep the ratio high. Over time the orphan pool grows without bound. [7](#0-6) [8](#0-7) 

### Impact Explanation

Unbounded growth of `InnerPool::blocks` and `InnerPool::parents` (both `HashMap`s held under a `RwLock`). Each orphan block is also written to the DB (`insert_block` before orphan pool insertion). Sustained attack causes node OOM and/or disk exhaustion, crashing the node or making it unresponsive. Impact: **High** (node memory/disk exhaustion, denial of service). [9](#0-8) 

### Likelihood Explanation

The P2P block relay path is fully open to any peer. No authentication, no stake, no rate-limit visible in this code path. Mining a block with `compact_target = 0x207fffff` takes milliseconds. The attacker only needs to send enough far-future-epoch siblings to keep the sampling ratio favorable. The 60-second cleanup timer means the attacker has ample time to replenish between cycles.

### Recommendation

1. **Fix `need_clean`**: check the **minimum** epoch across all direct children, not just the first iterated one:
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
   Or use `min()` over epochs and compare once.

2. **Add PoW verification to `non_contextual_verify`**: `PowVerifier` does not require the parent block and can be run non-contextually. This closes the trivial-mining vector for all orphan-pool abuse.

3. **Cap orphan pool size**: enforce a hard limit on `InnerPool::parents.len()` and reject insertions beyond it (with peer penalization).

### Proof of Concept

Construct a leader hash `L` (any unknown parent hash). Insert 100 blocks with `epoch=1` (expired at `tip_epoch=20`) as children of `L`. Insert 100 blocks with `epoch=10020` (far-future) as children of `L`. Call `clean_expired_blocks(20)` 10 000 times. Assert that in a significant fraction of calls `need_clean` returns `false` and the 100 expired blocks remain in the pool. Repeat with randomized insertion order to confirm the non-determinism is driven purely by HashMap iteration order. [10](#0-9)

### Citations

**File:** chain/src/utils/orphan_block_pool.rs (L99-122)
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

**File:** verification/src/block_verifier.rs (L39-47)
```rust
    fn verify(&self, target: &BlockView) -> Result<(), Error> {
        let max_block_proposals_limit = self.consensus.max_block_proposals_limit();
        let max_block_bytes = self.consensus.max_block_bytes();
        BlockProposalsLimitVerifier::new(max_block_proposals_limit).verify(target)?;
        BlockBytesVerifier::new(max_block_bytes).verify(target)?;
        CellbaseVerifier::new().verify(target)?;
        DuplicateVerifier::new().verify(target)?;
        MerkleRootVerifier::new().verify(target)
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

**File:** verification/src/header_verifier.rs (L32-50)
```rust
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

**File:** chain/src/orphan_broker.rs (L119-123)
```rust
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
        } else {
            self.orphan_blocks_broker.insert(lonely_block);
        }
```

**File:** chain/src/orphan_broker.rs (L134-155)
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
```
