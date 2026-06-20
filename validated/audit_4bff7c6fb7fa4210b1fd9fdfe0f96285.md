### Title
`InnerPool::need_clean` Samples Only One Sibling, Allowing Attacker to Permanently Suppress Orphan Cleanup — (`chain/src/utils/orphan_block_pool.rs`)

---

### Summary

`InnerPool::need_clean` checks only the first entry returned by `map.iter().next()` on the inner sibling `HashMap`. Because Rust's `HashMap` iteration order is non-deterministic across process restarts (and consistent within a run), an attacker who inserts a non-expired sibling block alongside an expired one under the same parent can permanently prevent the expired block from being evicted. Because non-contextual verification (the only gate before orphan pool insertion) checks neither PoW nor epoch continuity, the attacker can craft these blocks with zero mining cost.

---

### Finding Description

**The logical flaw — `need_clean`:** [1](#0-0) 

The function retrieves the inner `HashMap<block_hash, LonelyBlockHash>` for a given `parent_hash` and calls `.iter().next()` — returning exactly one, arbitrarily-ordered entry. It returns `true` (clean) only if that single sampled block satisfies `epoch + EXPIRED_EPOCH < tip_epoch`. If the sampled block is not expired, the function returns `false` and `clean_expired_blocks` skips the entire sibling group, leaving any expired siblings permanently in the pool. [2](#0-1) 

**The missing gate — non-contextual verification does not check PoW or epoch:**

`non_contextual_verify` calls only `BlockVerifier` and `NonContextualBlockTxsVerifier`: [3](#0-2) 

`BlockVerifier::verify` checks proposals limit, block bytes, cellbase structure, duplicates, and merkle root — **no PoW, no epoch number**: [4](#0-3) 

PoW and epoch continuity are only enforced in `HeaderVerifier`, which is a *contextual* verifier requiring the parent header — it is never invoked before orphan pool insertion: [5](#0-4) 

The epoch continuity check (`is_successor_of`) requires the parent's epoch and is therefore unreachable without the parent: [6](#0-5) 

---

### Impact Explanation

An attacker can insert an unbounded number of expired orphan blocks that are never evicted. Each expired block occupies memory in `InnerPool::blocks`, `InnerPool::parents`, and the associated `block_status_map` and header map. The cleanup timer fires every 60 seconds but is permanently defeated for any parent group that contains at least one non-expired sibling: [7](#0-6) 

Because `HashMap` iteration order is fixed within a process run (same hash seed), once the non-expired block wins the first `.iter().next()` race, it wins every subsequent one. The expired block is never removed. Repeating this across many distinct parent hashes causes monotonically growing memory consumption, leading to OOM.

---

### Likelihood Explanation

- **Entry point**: Standard P2P `SendBlock` relay message — no privileged access required.
- **Cost**: Zero mining work. `BlockVerifier` does not verify PoW; the attacker can set `nonce = 0` and any `epoch` field freely.
- **Determinism**: Within a single node process, `HashMap` iteration order is stable for a fixed key set. The attacker can insert multiple non-expired siblings to statistically guarantee one precedes the expired block in iteration order.
- **Repeatability**: The 60-second cleanup timer re-runs `need_clean` with the same consistent iteration order, so the expired block is never sampled as the first entry once a non-expired sibling precedes it.

---

### Recommendation

Replace the single-sample check with an `all()`-based scan: only skip cleanup if **every** block in the sibling group is non-expired, or alternatively clean the group if **any** block is expired:

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .map(|map| {
            map.values()
                .any(|b| b.epoch_number() + EXPIRED_EPOCH < tip_epoch)
        })
        .unwrap_or_default()
}
```

Additionally, add a non-contextual PoW check (verifying the hash meets the block's own stated `compact_target`) inside `BlockVerifier` to raise the cost of orphan pool spam.

---

### Proof of Concept

```rust
// Two siblings under the same parent:
// - block_expired: epoch = 1 (expired when tip_epoch = 8, since 1 + 6 < 8)
// - block_fresh:   epoch = 10 (not expired when tip_epoch = 8)
//
// Insert both. Run clean_expired_blocks(8) 1000 times.
// Assert block_expired is still present (need_clean sampled block_fresh).
let pool = OrphanBlockPool::with_capacity(10);
let parent_hash = /* some hash */;
pool.insert(make_block(parent_hash, epoch=1));   // expired
pool.insert(make_block(parent_hash, epoch=10));  // not expired

for _ in 0..1000 {
    pool.clean_expired_blocks(8);
}
// Within a single run, HashMap order is stable:
// if block_fresh precedes block_expired in iteration, expired block is never removed.
assert!(pool.len() >= 1); // expired block remains
```

### Citations

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
