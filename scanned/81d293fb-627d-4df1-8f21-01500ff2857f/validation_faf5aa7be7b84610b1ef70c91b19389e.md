Based on my thorough analysis of the code, here is my assessment:

---

### Title
Unbounded O(N) Leaders Scan in `search_orphan_leaders` Enables DoS via Orphan Block Flooding — (`chain/src/utils/orphan_block_pool.rs`, `chain/src/orphan_broker.rs`)

### Summary

An unprivileged remote attacker can send structurally valid blocks (no PoW required) with distinct unknown parent hashes via P2P. Each block creates a new entry in the `leaders` HashSet. Because `search_orphan_leaders` iterates the entire leaders set after every block processed, and the orphan pool has no hard size cap, the ChainService single-threaded loop degrades to O(N) per block, effectively stalling block processing for honest peers.

### Finding Description

**Step 1 — No PoW check in `non_contextual_verify`.**

`ChainService::asynchronous_process_block` calls `non_contextual_verify`, which invokes `BlockVerifier` and `NonContextualBlockTxsVerifier`. [1](#0-0) 

`BlockVerifier` is documented to contain only: CellbaseVerifier, BlockBytesVerifier, BlockExtensionVerifier, BlockProposalsLimitVerifier, DuplicateVerifier, MerkleRootVerifier — **no PoW check**. [2](#0-1) 

PoW is only verified inside `HeaderVerifier` (context-dependent, requires parent header), which is not called in the non-contextual path. [3](#0-2) 

**Step 2 — Every structurally valid block with an unknown parent is inserted into the orphan pool, creating a new leader.**

In `InnerPool::insert`, if `parent_hash` is not already in `self.parents`, it is unconditionally added to `self.leaders`. [4](#0-3) 

There is **no eviction, no size cap, no rate limit** in `insert`. The `with_capacity` argument is only a HashMap pre-allocation hint, not a maximum. [5](#0-4) 

**Step 3 — `search_orphan_leaders` iterates all N leaders after every single block.**

`process_lonely_block` unconditionally calls `search_orphan_leaders()` at the end, regardless of whether the block was orphaned or not. [6](#0-5) 

`search_orphan_leaders` clones the entire leaders set (O(N) allocation + copy) and then calls `search_orphan_leader` for each entry. [7](#0-6) 

For leaders whose parent is not stored and not pending verify (which is the case for all attacker-injected blocks), `search_orphan_leader` does two hash-map lookups and returns — so the full scan is O(N) per block processed. [8](#0-7) 

**Step 4 — The ChainService is single-threaded.**

All block processing runs sequentially in one thread. The 60-second cleanup timer only evicts blocks whose epoch is 6+ epochs behind the tip — attacker blocks with a current epoch number survive for ~24 hours on mainnet. [9](#0-8) 

The pool is initialized with `ORPHAN_BLOCK_SIZE = BLOCK_DOWNLOAD_WINDOW` (a hint, not a cap). [10](#0-9) 

### Impact Explanation

With N attacker blocks in the pool, each subsequent legitimate block triggers an O(N) leaders scan. For N = 10,000 blocks, the ChainService loop stalls processing legitimate blocks, causing the node to fall behind the chain tip and violating the invariant that sync state machines remain bounded and performant. This is a remote DoS against block processing throughput.

### Likelihood Explanation

The attack requires only a P2P connection. The attacker crafts blocks with:
- A valid cellbase transaction (trivial)
- A valid merkle root (trivial, computed from the cellbase)
- Distinct random unknown parent hashes (trivial)
- No valid PoW required

Each block passes `non_contextual_verify` and is written to the DB and orphan pool. The `new_block_received` dedup check only filters exact hash duplicates; since each block has a unique hash, it does not prevent the flood. [11](#0-10) 

### Recommendation

1. **Enforce a hard cap** on `InnerPool::leaders` (and correspondingly `blocks`/`parents`). When the cap is reached, reject new orphan insertions or evict the oldest leaders.
2. **Move `search_orphan_leaders` out of the hot path**: only call it when a block is successfully connected to the chain (i.e., when a new block is stored), not on every orphan insertion.
3. **Add PoW verification to `non_contextual_verify`** so that blocks without valid PoW are rejected before reaching the orphan pool.

### Proof of Concept

```
1. Connect to a CKB node via P2P.
2. For i in 0..10_000:
     Craft a block with:
       - parent_hash = random_unknown_hash_i  (distinct each time)
       - number = 1
       - valid cellbase tx
       - merkle_root = hash(cellbase)
       - any nonce (no PoW needed)
     Send via SendBlock P2P message.
3. Send one legitimate block.
4. Observe: ChainService takes O(10,000) iterations of search_orphan_leaders
   before processing the legitimate block.
5. Assert: processing time grows linearly with pool size, not bounded by a constant.
```

The `insert_block` DB write per attacker block is an additional amplifier but the primary bottleneck is the O(N) leaders scan on the single ChainService thread. [12](#0-11) [13](#0-12)

### Citations

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

**File:** verification/src/header_verifier.rs (L33-35)
```rust
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
        let parent_fields = self
```

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

**File:** chain/src/orphan_broker.rs (L52-59)
```rust
        let leader_is_pending_verify = self.is_pending_verify.contains(&leader_hash);
        if !leader_is_pending_verify && !leader_status.contains(BlockStatus::BLOCK_STORED) {
            trace!(
                "orphan leader: {} not stored {:?} and not in is_pending_verify: {}",
                leader_hash, leader_status, leader_is_pending_verify
            );
            return;
        }
```

**File:** chain/src/orphan_broker.rs (L74-78)
```rust
    fn search_orphan_leaders(&self) {
        for leader_hash in self.orphan_blocks_broker.clone_leaders() {
            self.search_orphan_leader(leader_hash);
        }
    }
```

**File:** chain/src/orphan_broker.rs (L122-125)
```rust
            self.orphan_blocks_broker.insert(lonely_block);
        }

        self.search_orphan_leaders();
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

**File:** sync/src/synchronizer/block_process.rs (L43-77)
```rust
        if shared.new_block_received(&block) {
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
