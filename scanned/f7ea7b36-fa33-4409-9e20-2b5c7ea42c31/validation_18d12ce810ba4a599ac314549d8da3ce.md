Audit Report

## Title
Unbounded `OrphanBlockPool` Growth via Parentless Block Flooding — (`chain/src/utils/orphan_block_pool.rs`)

## Summary
`InnerPool::with_capacity` is a Rust `HashMap` pre-allocation hint only — it does not enforce any insertion cap. `InnerPool::insert` performs zero size checks before inserting into all three data structures (`blocks`, `parents`, `leaders`). The sole eviction path is epoch-based expiry triggered every 60 seconds, which is trivially bypassed by setting a block's epoch field to `>= tip_epoch - EXPIRED_EPOCH`. A remote peer that can produce syntactically valid blocks with distinct unknown parent hashes can grow the orphan pool without bound, eventually OOM-crashing the node.

## Finding Description

**Root cause — `with_capacity` is not a cap:**

`InnerPool::with_capacity` calls `HashMap::with_capacity(capacity)`, which is a Rust memory pre-allocation hint only. No field stores the capacity value for later enforcement. [1](#0-0) 

`InnerPool::insert` performs zero size checks before inserting into all three data structures: [2](#0-1) 

The production pool is initialized with `ORPHAN_BLOCK_SIZE = BLOCK_DOWNLOAD_WINDOW as usize = 8192` — again, only a hint: [3](#0-2) 

**Eviction bypass:**

The only eviction path is a 60-second timer calling `clean_expired_orphans`: [4](#0-3) 

The expiry condition is `epoch_number + EXPIRED_EPOCH < tip_epoch` where `EXPIRED_EPOCH = 6`. Blocks with epoch field set to `>= tip_epoch - 6` are never expired. `BlockVerifier` (the non-contextual verifier) does not check epoch continuity against live chain state — it only checks proposals limit, block bytes, cellbase structure, duplicates, and merkle root: [5](#0-4) [6](#0-5) 

**Reachable attack path:**

Remote peer → sync protocol → `asynchronous_process_block` → `non_contextual_verify` (passes) → `insert_block` (DB write) → `orphan_broker.process_lonely_block`: [7](#0-6) 

Because the parent hash is unknown (not stored, not pending verify, not invalid), the block falls into the unconditional `insert` branch: [8](#0-7) 

Each block with a unique parent hash adds one entry to `blocks`, one to `parents`, and one to `leaders` — all unbounded. The block is also written to the DB via `insert_block`, adding disk pressure alongside memory pressure. [9](#0-8) 

**Contrast with the tx orphan pool**, which does enforce a hard cap (`DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`) with explicit eviction in `limit_size`: [10](#0-9) 

The block orphan pool has no equivalent protection.

## Impact Explanation

Continuous insertion of N orphan blocks with distinct parent hashes and current-epoch numbers causes linear memory growth in `InnerPool::blocks`, `::parents`, and `::leaders` with no eviction. The process is eventually OOM-killed. This maps to: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The attack requires producing syntactically valid blocks that pass `BlockVerifier` (proposals limit, block bytes, cellbase, duplicates, merkle root) and valid PoW (verified at the header stage in the sync protocol before blocks reach `asynchronous_process_block`). On mainnet this requires real hashpower per block, making a sustained flood expensive but not requiring majority hashpower. On testnet/devnet (low difficulty) the attack is trivially cheap. The epoch-expiry bypass is unconditional and requires no special capability beyond setting the epoch field in the block header. `BLOCK_DOWNLOAD_WINDOW = 8192` is the nominal "capacity" hint; exceeding it requires only 8193+ valid blocks, which is a realistic sustained attack volume. [11](#0-10) 

## Recommendation

1. **Store capacity as a field on `InnerPool`** rather than discarding it after `HashMap::with_capacity`.
2. **Enforce a hard cap in `InnerPool::insert`**: check `self.parents.len() >= self.capacity` before inserting; when the cap is reached, evict the oldest leader and all its descendants (or drop the incoming block).
3. **Add per-peer orphan block accounting** to limit how many orphans a single peer can contribute, mirroring the per-peer limits used for unknown transaction hashes (`MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`).
4. **Tighten epoch range checks** in non-contextual verification to reject blocks whose epoch number is implausibly far from the current tip.

## Proof of Concept

```rust
// Minimal reproduction: insert N blocks with distinct parent hashes, current epoch
let pool = OrphanBlockPool::with_capacity(BLOCK_DOWNLOAD_WINDOW as usize);
let tip_epoch = shared.snapshot().tip_header().epoch().number();
for i in 0..100_000u64 {
    let block = BlockBuilder::default()
        .parent_hash(random_byte32())          // unique unknown parent → new leader each time
        .epoch(EpochNumberWithFraction::new(tip_epoch, i % 1000, 1000))
        .number(tip_epoch * 1000 + i)
        // valid cellbase, valid merkle root, valid PoW
        .build();
    pool.insert(block.into());
}
assert_eq!(pool.len(), 100_000);  // no eviction occurred
// RSS grows proportionally; no bound is enforced
```

The `test_remove_expired_blocks` test in `chain/src/tests/orphan_block_pool.rs` already demonstrates that blocks with epoch set to a fixed deprecated value are cleaned up — but it does not test the case where epoch is set to `>= tip_epoch`, confirming the bypass is untested and present. [12](#0-11)

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

**File:** chain/src/chain_service.rs (L146-151)
```rust
    fn insert_block(&self, lonely_block: &LonelyBlock) -> Result<(), ckb_error::Error> {
        let db_txn = self.shared.store().begin_transaction();
        db_txn.insert_block(lonely_block.block())?;
        db_txn.commit()?;
        Ok(())
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

**File:** tx-pool/src/component/orphan.rs (L96-132)
```rust
    fn limit_size(&mut self) -> Vec<Byte32> {
        let now = ckb_systemtime::unix_time().as_secs();
        let expires: Vec<_> = self
            .entries
            .iter()
            .filter_map(|(id, entry)| {
                if entry.expires_at <= now {
                    Some(id)
                } else {
                    None
                }
            })
            .cloned()
            .collect();

        let mut evicted_txs = vec![];

        for id in expires {
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        if !evicted_txs.is_empty() {
            trace!("OrphanTxPool full, evicted {} tx", evicted_txs.len());
            self.shrink_to_fit();
        }
        evicted_txs
    }
```

**File:** util/constant/src/sync.rs (L54-54)
```rust
pub const BLOCK_DOWNLOAD_WINDOW: u64 = 1024 * 8; // 1024 * default_outbound_peers
```

**File:** chain/src/tests/orphan_block_pool.rs (L232-263)
```rust
#[test]
fn test_remove_expired_blocks() {
    let consensus = ConsensusBuilder::default().build();
    let block_number = 20;
    let mut parent = consensus.genesis_block().header();
    let pool = OrphanBlockPool::with_capacity(block_number);

    let deprecated = EpochNumberWithFraction::new(10, 0, 10);

    for _ in 1..block_number {
        let new_block = BlockBuilder::default()
            .parent_hash(parent.hash())
            .timestamp(unix_time_as_millis())
            .number(parent.number() + 1)
            .epoch(deprecated)
            .nonce(parent.nonce() + 1)
            .build();

        parent = new_block.header();
        let lonely_block = LonelyBlock {
            block: Arc::new(new_block),
            switch: None,
            verify_callback: None,
        };
        pool.insert(lonely_block.into());
    }
    assert_eq!(pool.leaders_len(), 1);

    let v = pool.clean_expired_blocks(20_u64);
    assert_eq!(v.len(), 19);
    assert_eq!(pool.leaders_len(), 0);
}
```
