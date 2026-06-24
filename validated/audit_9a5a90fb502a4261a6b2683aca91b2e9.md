Audit Report

## Title
Unbounded Orphan Block Pool Growth via PoW-Free Structurally Valid Blocks Causes Remote OOM — (`chain/src/utils/orphan_block_pool.rs`, `chain/src/orphan_broker.rs`)

## Summary

`OrphanBlockPool::with_capacity` pre-allocates HashMap memory but enforces no insertion cap. A remote peer can send an unlimited number of structurally valid, PoW-invalid blocks with unique unknown parent hashes. Each block passes `non_contextual_verify`, is written to the database via `insert_block`, and is inserted into the orphan pool. The pool grows without bound, exhausting process memory and causing an OOM kill of the CKB node.

## Finding Description

**No hard cap in `OrphanBlockPool::insert`:**

`InnerPool::with_capacity` calls only `HashMap::with_capacity`, which is a pre-allocation hint, not an insertion limit. [1](#0-0) 

`InnerPool::insert` performs no size check before inserting into `blocks`, `parents`, and `leaders`. [2](#0-1) 

The pool is initialized with `ORPHAN_BLOCK_SIZE = BLOCK_DOWNLOAD_WINDOW as usize` (8192) — purely advisory, not enforced. [3](#0-2) 

**PoW is not checked in `non_contextual_verify`:**

`ChainService::non_contextual_verify` runs only `BlockVerifier` and `NonContextualBlockTxsVerifier`. [4](#0-3) 

`BlockVerifier::verify` checks proposals limit, block bytes, cellbase structure, duplicates, and merkle root — no PoW check. [5](#0-4) 

PoW verification lives in `PowVerifier` inside `HeaderVerifier`, which is a contextual verifier requiring the parent header. It is never invoked on the orphan path. [6](#0-5) 

**The orphan insertion path:**

After passing `non_contextual_verify` and `insert_block` (DB write), `process_lonely_block` reaches the `else` branch and calls `orphan_blocks_broker.insert(lonely_block)` with no capacity guard. [7](#0-6) 

**The only eviction mechanism is epoch-based, not count-based:**

`clean_expired_orphans` (called every 60 s) only removes blocks where `epoch_number + EXPIRED_EPOCH (6) < tip_epoch`. Attacker blocks stamped with the current epoch survive for ~6 epochs. [8](#0-7) 

**No peer ban for orphan blocks:**

The verify callback in `BlockProcess::execute` only bans a peer when full contextual verification fails. Orphan blocks never reach full verification — they wait in the pool for their nonexistent parent — so the sending peer is never penalized. [9](#0-8) 

## Impact Explanation

Each malicious block consumes memory in `InnerPool.blocks`, `InnerPool.parents`, and `InnerPool.leaders`, plus a DB write via `insert_block`. With no count cap and no PoW requirement, an attacker can grow the orphan pool to an arbitrary size, exhausting process memory and triggering an OOM kill — a remote crash of the CKB node. This matches the allowed High impact: **"Vulnerabilities which could easily crash a CKB node."**

## Likelihood Explanation

- Requires only a standard P2P connection (no privilege, no key, no hashpower).
- Block construction is trivial: valid cellbase + correct merkle root + arbitrary unknown parent hash + any nonce (PoW not checked on the orphan path).
- The `process_block_tx` channel (size 24) limits per-batch throughput but not total accumulation.
- The attacker can sustain the flood indefinitely; the peer is never banned.
- Epoch-based cleanup provides no protection within the current epoch window (~hours).

## Recommendation

1. **Enforce a hard count cap in `OrphanBlockPool::insert`**: reject or evict the oldest/random entry when `parents.len() >= capacity`.
2. **Add PoW verification before orphan insertion** (e.g., in `non_contextual_verify` or as a pre-check in `process_lonely_block`) so that crafting each block requires real work.
3. **Add per-peer orphan rate limiting** and ban peers that contribute a disproportionate share of orphan blocks.
4. **Restore the IBD orphan pool size limit** (referenced in CHANGELOG #4381, v0.115.0) which appears not to have been carried forward into the async-sync architecture introduced in v0.118.0.

## Proof of Concept

```rust
// Pseudocode — run against a local devnet node
for i in 0..BLOCK_DOWNLOAD_WINDOW * 10 {
    let fake_parent = random_byte32();          // unknown parent → always orphaned
    let block = build_minimal_valid_block(      // passes BlockVerifier (no PoW check)
        fake_parent,
        current_epoch,
        /* nonce = */ 0,
    );
    p2p_send(SendBlock(block));                 // via sync protocol
}
// Assert: orphan_pool.len() >> BLOCK_DOWNLOAD_WINDOW (8192)
// Observable via ckb_chain_orphan_count metric or node OOM kill
```

The test can be confirmed by monitoring the `ckb_chain_orphan_count` metric: it will grow linearly with no bound, far exceeding the nominal 8192 capacity hint, until the process is OOM-killed.

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

**File:** sync/src/synchronizer/block_process.rs (L44-77)
```rust
            let verify_callback = {
                let nc: Arc<dyn CKBProtocolContext + Sync> = Arc::clone(&self.nc);
                let peer_id: PeerIndex = self.peer;
                let block_hash: Byte32 = block.hash();
                Box::new(move |verify_result: Result<bool, ckb_error::Error>| {
                    match verify_result {
                        Ok(_) => {}
                        Err(err) => {
                            let is_internal_db_error = is_internal_db_error(&err);
                            if is_internal_db_error {
                                return;
                            }

                            // punish the malicious peer
                            post_sync_process(
                                nc.as_ref(),
                                peer_id,
                                "SendBlock",
                                StatusCode::BlockIsInvalid.with_context(format!(
                                    "block {} is invalid, reason: {}",
                                    block_hash, err
                                )),
                            );
                        }
                    };
                })
            };
            let remote_block = RemoteBlock {
                block,
                verify_callback,
            };
            self.synchronizer
                .asynchronous_process_remote_block(remote_block);
        }
```
