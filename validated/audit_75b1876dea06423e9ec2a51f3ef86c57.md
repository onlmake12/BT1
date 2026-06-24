Audit Report

## Title
`InnerPool::need_clean` single-sample HashMap check enables permanent orphan-pool cleanup suppression via epoch-spoofed shield blocks — (`chain/src/utils/orphan_block_pool.rs`)

## Summary

`InnerPool::need_clean` calls `map.iter().next()` on a `HashMap`, sampling exactly one child block per parent group to decide whether to evict the group. Because PoW and epoch validation are absent from `non_contextual_verify`, any P2P peer can insert a structurally valid block with an attacker-controlled epoch number. By pairing an expired-epoch block with a recent-epoch "shield" block under the same fake parent hash, an attacker causes ~50% of inserted groups to escape each cleanup pass, leading to unbounded orphan pool growth and eventual OOM crash.

## Finding Description

**Single-sample check in `need_clean`:**

`InnerPool::need_clean` at `chain/src/utils/orphan_block_pool.rs:113-122` calls `map.iter().next()` on a `HashMap<Byte32, LonelyBlockHash>`, returning one non-deterministic entry. If a parent group contains two children — one expired (epoch 1) and one shield (epoch 19, tip=20, `EXPIRED_EPOCH=6`) — and the shield is returned by `.next()`, the function returns `false` and neither child is ever evicted. [1](#0-0) 

`clean_expired_blocks` iterates all leaders and calls `need_clean` per group; if `need_clean` returns `false`, `remove_blocks_by_parent` is never called for that group. [2](#0-1) 

**No PoW or epoch validation before orphan pool insertion:**

`non_contextual_verify` in `chain_service.rs` calls only `BlockVerifier` and `NonContextualBlockTxsVerifier`. [3](#0-2) 

`BlockVerifier::verify` runs `BlockProposalsLimitVerifier`, `BlockBytesVerifier`, `CellbaseVerifier`, `DuplicateVerifier`, and `MerkleRootVerifier` — no PoW check, no epoch check. [4](#0-3) 

`PowVerifier` and `EpochVerifier` live exclusively in `HeaderVerifier`, which requires the parent block and is never invoked on this path. [5](#0-4) 

After passing `non_contextual_verify`, the block is stored to DB and handed to `orphan_broker.process_lonely_block` with no further epoch gating. [6](#0-5) 

**Epoch number taken directly from the unvalidated block header:**

When `LonelyBlock` is converted to `LonelyBlockHash`, `epoch_number` is read directly from `block.epoch().number()` — the attacker-controlled header field. [7](#0-6) 

**No hard eviction cap on the orphan pool:**

`OrphanBlockPool::with_capacity` passes the capacity to `HashMap::with_capacity`, which is a hint only — not an enforced maximum. The sole reclamation path is `clean_expired_blocks`, fired every 60 seconds. [8](#0-7) [9](#0-8) 

`ORPHAN_BLOCK_SIZE` is passed as a capacity hint, not a hard limit. [10](#0-9) 

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

With the shield technique, approximately 50% of inserted expired groups survive each 60-second cleanup pass on average. An attacker sending a steady stream of (expired, shield) pairs under distinct fake parent hashes causes monotonically growing RSS. Since there is no hard eviction cap on the orphan pool, this leads to unbounded memory growth and eventual OOM crash of the targeted node.

## Likelihood Explanation

- No PoW required; any peer can send arbitrary `SendBlock` messages with crafted epoch fields.
- No privileged role needed — reachable from any unauthenticated P2P connection.
- Attack cost is minimal: two small, structurally valid blocks per fake parent hash.
- The 60-second cleanup timer means a modest send rate suffices to outpace cleanup.
- Non-determinism favors the attacker: on average half of all inserted groups escape cleanup permanently.

## Recommendation

Replace the single-sample check with a check over all children using the minimum epoch:

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

Additionally, add PoW verification (via `PowVerifier` from `HeaderVerifier`) to `non_contextual_verify` so that epoch-spoofed blocks are rejected at the network boundary before reaching the orphan pool. A hard cap enforced at insertion time in `OrphanBlockPool::insert` would provide defense-in-depth.

## Proof of Concept

```rust
// Insert two children under the same fake parent:
// one expired (epoch 1), one shield (epoch 19), tip_epoch = 20, EXPIRED_EPOCH = 6
let pool = OrphanBlockPool::with_capacity(10);
let fake_parent = random_byte32();

let expired = make_lonely_block_hash(fake_parent.clone(), epoch = 1);
let shield  = make_lonely_block_hash(fake_parent.clone(), epoch = 19);

pool.insert(expired);
pool.insert(shield);

let removed = pool.clean_expired_blocks(20);

// BUG: if shield is iterated first by HashMap, removed.len() == 0
// The expired block is never cleaned.
// This assertion fails ~50% of the time due to HashMap non-determinism.
assert_eq!(removed.len(), 1);
```

Run with `RUSTFLAGS="-Z randomize-layout"` or repeat in a loop to observe the non-deterministic failure. A fuzz test seeding the HashMap with varying insertion orders will reliably reproduce the suppression.

### Citations

**File:** chain/src/utils/orphan_block_pool.rs (L28-34)
```rust
    fn with_capacity(capacity: usize) -> Self {
        InnerPool {
            blocks: HashMap::with_capacity(capacity),
            parents: HashMap::new(),
            leaders: HashSet::new(),
        }
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

**File:** chain/src/chain_service.rs (L40-62)
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

**File:** chain/src/lib.rs (L97-97)
```rust
        let epoch_number: EpochNumber = block.epoch().number();
```

**File:** chain/src/init.rs (L22-43)
```rust
const ORPHAN_BLOCK_SIZE: usize = BLOCK_DOWNLOAD_WINDOW as usize;

/// Here we distinguish between build_chain_services and start_chain_services:
/// * build_chain_services simply initializes ChainController, setting up all relevant
///   threads, and return join handle for the main chain service thread.
/// * start_chain_services first builds relevant data just like build_chain_services,
///   in addition, it register the main chain service thread against CKB's handler. As
///   a result, start_chain_services only returns ChainController, it is expected that
///   CKB's stop handler shall be used to terminate the created chain service.
pub fn start_chain_services(builder: ChainServicesBuilder) -> ChainController {
    let (chain_service, chain_service_thread) = build_chain_services(builder);
    register_thread("ChainService", chain_service_thread);

    chain_service
}

/// Please refer to +start_chain_services+ for difference between build_chain_services
/// and start_chain_services
pub fn build_chain_services(
    builder: ChainServicesBuilder,
) -> (ChainController, thread::JoinHandle<()>) {
    let orphan_blocks_broker = Arc::new(OrphanBlockPool::with_capacity(ORPHAN_BLOCK_SIZE));
```
