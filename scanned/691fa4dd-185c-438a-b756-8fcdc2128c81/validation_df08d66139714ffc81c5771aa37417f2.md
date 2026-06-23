The code evidence is decisive. Let me trace every layer of the claimed path.

**`BlockVerifier` does NOT check PoW:** [1](#0-0) 

It only checks: proposals limit, block bytes, cellbase structure, duplicates, and merkle root. No PoW.

**PoW lives in `HeaderVerifier`** (contextual, requires parent): [2](#0-1) 

`HeaderVerifier::verify` calls `PowVerifier` first, but it also requires `data_loader.get_header_fields(&header.parent_hash())` — unavailable for orphan blocks with unknown parents.

**`non_contextual_verify` in `ChainService` only calls `BlockVerifier` + `NonContextualBlockTxsVerifier`:** [3](#0-2) 

No PoW check. A block with a fabricated nonce passes this gate.

**`OrphanBlockPool::insert` has no hard size limit:** [4](#0-3) 

`with_capacity` in `init.rs` is only a pre-allocation hint: [5](#0-4) 

`HashMap::with_capacity` does not cap insertions. The pool grows without bound.

**The only eviction is a 60-second epoch-based timer:** [6](#0-5) 

`clean_expired_orphans` only removes blocks whose `epoch_number + EXPIRED_EPOCH < tip_epoch` (EXPIRED_EPOCH = 6): [7](#0-6) 

During IBD the tip epoch advances slowly; freshly-injected orphan blocks are never expired within the attack window.

**The full call chain (no PoW barrier):** [8](#0-7) 

`asynchronous_process_block` → `non_contextual_verify` (no PoW) → `insert_block` (DB write) → `orphan_broker.process_lonely_block` → `orphan_blocks_broker.insert` (unbounded).

---

### Title
Unbounded `OrphanBlockPool` Growth via PoW-Free Orphan Block Flooding — (`chain/src/utils/orphan_block_pool.rs`, `chain/src/chain_service.rs`)

### Summary
An unprivileged remote peer can exhaust node RAM by relaying syntactically valid blocks whose parent hashes are all distinct and unknown, because `non_contextual_verify` does not check PoW and `OrphanBlockPool::insert` enforces no size cap.

### Finding Description
`ChainService::asynchronous_process_block` runs `BlockVerifier` + `NonContextualBlockTxsVerifier` as its only gate. `BlockVerifier` checks proposals limit, block bytes, cellbase structure, duplicates, and merkle root — **not PoW**. PoW lives in `HeaderVerifier`, which is contextual (requires the parent header) and is never invoked on this path. After passing that gate, the block is written to the DB and handed to `OrphanBroker::process_lonely_block`. Because the parent is unknown, the block is unconditionally inserted into `OrphanBlockPool` via `InnerPool::insert`, which appends to three unbounded `HashMap`/`HashSet` structures (`blocks`, `parents`, `leaders`) with no eviction. The only cleanup is a 60-second ticker that removes blocks 6+ epochs behind the tip — irrelevant for freshly-injected orphans during IBD.

### Impact Explanation
Each inserted orphan consumes heap memory for the `LonelyBlockHash` struct plus three map entries. At N = 10 000 distinct-parent orphans injected within 59 seconds, all three `InnerPool` maps grow to N entries with no bound. On a node with limited RAM this causes OOM; on a well-provisioned node it causes severe GC pressure and lock contention on the `RwLock<InnerPool>`, degrading block processing for legitimate peers.

### Likelihood Explanation
Crafting a block that passes `BlockVerifier` requires only: a valid cellbase transaction, correct merkle root, block size within limits, and no duplicate transactions. No mining is needed. A single attacker with a scripted client can generate and relay thousands of such blocks per minute over the P2P `SendBlock` message path.

### Recommendation
Enforce a hard count cap inside `OrphanBlockPool::insert`: if `self.parents.len() >= capacity`, evict the oldest entry (by insertion order or epoch) before inserting the new one. The `with_capacity` hint must be replaced with an enforced maximum. Additionally, consider adding PoW verification to `non_contextual_verify` (it is non-contextual since the difficulty target is embedded in `compact_target` of the block header itself).

### Proof of Concept
```python
# Pseudocode: flood orphan pool with PoW-free blocks
for i in range(10_000):
    block = build_block(
        parent_hash=random_bytes(32),   # unknown parent
        cellbase=valid_cellbase(i),
        merkle_root=compute_merkle([cellbase]),
        nonce=0,                        # invalid PoW, not checked
    )
    p2p_send(node, SendBlock(block))
# Measure RSS growth; assert RSS >> BLOCK_DOWNLOAD_WINDOW * sizeof(LonelyBlockHash)
```

### Citations

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

**File:** chain/src/chain_service.rs (L92-144)
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
    }
```

**File:** chain/src/utils/orphan_block_pool.rs (L36-54)
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
