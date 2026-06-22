Based on my thorough investigation of the code, this is a **real vulnerability**.

---

### Title
Unbounded OrphanBlockPool Growth via PoW-Free Block Flooding — (`chain/src/utils/orphan_block_pool.rs`, `chain/src/chain_service.rs`)

### Summary

An unprivileged remote peer can flood a CKB node with syntactically valid blocks (each with a unique unknown parent hash) that pass `non_contextual_verify` **without any PoW work**, causing `OrphanBlockPool` to grow without bound and exhaust heap memory.

### Finding Description

**Root cause 1: `non_contextual_verify` does not check PoW.**

`ChainService::non_contextual_verify` calls only `BlockVerifier` and `NonContextualBlockTxsVerifier`: [1](#0-0) 

`BlockVerifier::verify` runs only these checks — no `PowVerifier`: [2](#0-1) 

PoW is verified only inside `HeaderVerifier` (a *contextual* verifier requiring the parent to be known): [3](#0-2) 

An attacker can craft blocks with a valid cellbase, valid merkle root, valid size, and a random parent hash — all without performing any Eaglesong PoW work — and they will pass `non_contextual_verify`.

**Root cause 2: `OrphanBlockPool::insert` has no hard size limit.**

`ORPHAN_BLOCK_SIZE` is set to `BLOCK_DOWNLOAD_WINDOW = 8192`: [4](#0-3) 

`with_capacity` is passed to `HashMap::with_capacity`, which is advisory-only (pre-allocation hint, not a cap). `InnerPool::insert` performs zero size checks before inserting: [5](#0-4) 

The pool's three structures — `blocks`, `parents`, and `leaders` — all grow without bound.

**Root cause 3: The expiry cleanup is ineffective against a live attacker.**

`clean_expired_orphans` runs every 60 seconds and only removes blocks where `epoch_number + EXPIRED_EPOCH (6) < tip_epoch`: [6](#0-5) [7](#0-6) 

An attacker crafting blocks with the current epoch number will not be cleaned for ~6 epochs (~24 hours on mainnet).

**Attack path:**

```
P2P block relay
  → process_block_rx (bounded(24), just a queue)
  → asynchronous_process_block
  → non_contextual_verify  ← passes without PoW
  → insert_block           ← also writes to DB (disk exhaustion side-effect)
  → process_lonely_block
  → orphan_blocks_broker.insert()  ← no size limit, unbounded growth
``` [8](#0-7) [9](#0-8) 

### Impact Explanation

Each inserted orphan block consumes heap memory for three `HashMap` entries (`blocks`, `parents`, `leaders`). With no eviction policy and no hard cap, a sustained flood of crafted blocks causes unbounded RSS growth, ultimately triggering an OOM kill of the node process. Additionally, `insert_block` writes each block to RocksDB before orphan insertion, so disk space is also exhausted in parallel.

### Likelihood Explanation

The attack requires no PoW, no privileged access, and no Sybil capability. A single peer connection is sufficient. Crafting a valid block (correct cellbase, merkle root, size) is computationally trivial. The `process_block_rx` channel (size 24) throttles throughput slightly but does not prevent the attack — it only slows the rate of insertion.

### Recommendation

1. **Enforce a hard size limit in `InnerPool::insert`**: reject (or evict oldest) entries when `parents.len() >= capacity`.
2. **Check PoW in `non_contextual_verify`**: add `PowVerifier` to `BlockVerifier` so blocks without valid PoW are rejected before reaching the orphan pool or the database.
3. **Add per-peer rate limiting** on block submissions at the sync layer.
4. **Do not write to the database** (`insert_block`) before the block's parent is known to be reachable.

### Proof of Concept

```rust
// Craft N blocks each with a unique random parent hash
for _ in 0..N {
    let random_parent = Byte32::from(rand::random::<[u8; 32]>());
    let block = BlockBuilder::default()
        .parent_hash(random_parent)
        .number(1u64.pack())
        .epoch(EpochNumberWithFraction::new(current_epoch, 0, 1000).pack())
        .timestamp(unix_time_as_millis().pack())
        // valid cellbase + merkle root — no PoW needed
        .build_unchecked();
    peer.send_block(block); // via P2P CompactBlock or SendBlock message
}
// Monitor: pool.len() grows to N with no eviction
// Assert: node RSS grows proportionally; OOM kill at large N
```

`ORPHAN_BLOCK_SIZE = BLOCK_DOWNLOAD_WINDOW = 8192` [10](#0-9) 

Submitting `BLOCK_DOWNLOAD_WINDOW * 10 = 81920` blocks is sufficient to demonstrate unbounded growth well past the nominal capacity hint.

### Citations

**File:** chain/src/chain_service.rs (L40-42)
```rust
        let clean_expired_orphan_timer =
            crossbeam::channel::tick(std::time::Duration::from_secs(60));

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

**File:** chain/src/chain_service.rs (L92-143)
```rust
    fn asynchronous_process_block(&self, lonely_block: LonelyBlock) {
        let block_number = lonely_block.block().number();
        let block_hash = lonely_block.block().hash();
        // Skip verifying a genesis block if its hash is equal to our genesis hash,
        // otherwise, return error and ban peer.
        if block_number < 1 {
            if self.shared.genesis_hash() != block_hash {
                warn!(
                    "receive 0 number block: 0-{}, expect genesis hash: {}",
                    block_hash,
                    self.shared.genesis_hash()
                );
                self.shared
                    .insert_block_status(lonely_block.block().hash(), BlockStatus::BLOCK_INVALID);
                let error = InternalErrorKind::System
                    .other("Invalid genesis block received")
                    .into();
                lonely_block.execute_callback(Err(error));
            } else {
                warn!("receive 0 number block: 0-{}", block_hash);
                lonely_block.execute_callback(Ok(false));
            }
            return;
        }

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

**File:** chain/src/utils/orphan_block_pool.rs (L36-53)
```rust
    fn insert(&mut self, lonely_block: LonelyBlockHash) {
        let hash = lonely_block.hash();
        let parent_hash = lonely_block.parent_hash();
        self.blocks
            .entry(parent_hash.clone())
            .or_default()
            .insert(hash.clone(), lonely_block);
        // Out-of-order insertion needs to be deduplicated
        self.leaders.remove(&hash);
        // It is a possible optimization to make the judgment in advance,
        // because the parent of the block must not be equal to its own hash,
        // so we can judge first, which may reduce one arc clone
        if !self.parents.contains_key(&parent_hash) {
            // Block referenced by `parent_hash` is not in the pool,
            // and it has at least one child, the new inserted block, so add it to leaders.
            self.leaders.insert(parent_hash.clone());
        }
        self.parents.insert(hash, parent_hash);
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

**File:** chain/src/orphan_broker.rs (L107-132)
```rust
    pub(crate) fn process_lonely_block(&self, lonely_block: LonelyBlockHash) {
        let block_hash = lonely_block.block_number_and_hash.hash();
        let block_number = lonely_block.block_number_and_hash.number();
        let parent_hash = lonely_block.parent_hash();
        let parent_is_pending_verify = self.is_pending_verify.contains(&parent_hash);
        let parent_status = self.shared.get_block_status(&parent_hash);
        if parent_is_pending_verify || parent_status.contains(BlockStatus::BLOCK_STORED) {
            debug!(
                "parent {} has stored: {:?} or is_pending_verify: {}, processing descendant directly {}-{}",
                parent_hash, parent_status, parent_is_pending_verify, block_number, block_hash,
            );
            self.process_descendant(lonely_block);
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
        } else {
            self.orphan_blocks_broker.insert(lonely_block);
        }

        self.search_orphan_leaders();

        if let Some(metrics) = ckb_metrics::handle() {
            metrics
                .ckb_chain_orphan_count
                .set(self.orphan_blocks_broker.len() as i64)
        }
    }
```

**File:** util/constant/src/sync.rs (L54-54)
```rust
pub const BLOCK_DOWNLOAD_WINDOW: u64 = 1024 * 8; // 1024 * default_outbound_peers
```
