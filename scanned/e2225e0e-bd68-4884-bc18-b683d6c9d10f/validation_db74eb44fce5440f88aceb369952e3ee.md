### Title
Unbounded `OrphanBlockPool` Growth via Parentless Block Flooding — (`chain/src/utils/orphan_block_pool.rs`)

### Summary

`InnerPool::insert` enforces no capacity limit. The `with_capacity` constructor only pre-allocates `HashMap` memory; it does not cap insertions. A remote peer that can produce syntactically valid blocks (passing `non_contextual_verify`) with distinct, unknown parent hashes and non-expired epoch numbers can grow `OrphanBlockPool::blocks`, `::parents`, and `::leaders` without bound, exhausting node memory.

---

### Finding Description

**`with_capacity` is not a cap.**

`InnerPool::with_capacity` calls `HashMap::with_capacity(capacity)`, which is a Rust pre-allocation hint only: [1](#0-0) 

`InnerPool::insert` performs zero size checks before inserting into all three data structures: [2](#0-1) 

The production pool is initialized with `ORPHAN_BLOCK_SIZE = BLOCK_DOWNLOAD_WINDOW as usize` — again, only a hint: [3](#0-2) 

**The only eviction path is epoch-based expiry**, triggered by a 60-second timer: [4](#0-3) 

The expiry condition is `epoch_number + EXPIRED_EPOCH < tip_epoch` where `EXPIRED_EPOCH = 6`: [5](#0-4) 

An attacker sets the epoch field of crafted blocks to `>= current_tip_epoch`. Because `non_contextual_verify` does not check epoch continuity against the live chain state (that is a contextual check requiring the parent), these blocks pass verification and are never expired.

**Attack path (P2P → orphan pool):**

A remote peer sends a block via the sync protocol → `asynchronous_process_remote_block` → `asynchronous_process_lonely_block` → `ChainService::asynchronous_process_block`: [6](#0-5) 

After passing `non_contextual_verify` and `insert_block` (DB write), the block reaches `OrphanBroker::process_lonely_block`. Because the parent hash is unknown, it falls into the unconditional `insert` branch: [7](#0-6) 

Each block with a unique parent hash adds one entry to `blocks`, one to `parents`, and one to `leaders` — all unbounded.

---

### Impact Explanation

Continuous insertion of N orphan blocks with distinct parent hashes and current-epoch numbers causes:
- `InnerPool::blocks`: N outer entries, each with 1 inner entry
- `InnerPool::parents`: N entries
- `InnerPool::leaders`: N entries

No eviction occurs. Memory grows linearly with N until the process is OOM-killed. Each block is also written to the DB (`insert_block`), adding disk pressure.

---

### Likelihood Explanation

**Critical constraint — PoW is required.** `BlockVerifier` (called inside `non_contextual_verify`) includes Proof-of-Work verification, which is non-contextual in CKB. An attacker must solve valid PoW for each block. This makes the attack:

- **Trivially cheap** on testnet/devnet (low difficulty)
- **Expensive but feasible** on mainnet for a well-resourced attacker — does not require majority hashpower, only enough to generate a sustained stream of valid blocks

The epoch-expiry bypass (setting epoch ≥ tip_epoch) is unconditional and requires no special capability.

---

### Recommendation

1. **Enforce a hard capacity cap in `InnerPool::insert`**: check `self.parents.len() >= capacity` before inserting and either drop or evict (e.g., remove a random leader and its descendants) when the cap is reached.
2. **Store the capacity** as a field on `InnerPool` rather than discarding it after `HashMap::with_capacity`.
3. **Add per-peer orphan block accounting** to limit how many orphans a single peer can contribute.
4. **Tighten epoch range checks** in non-contextual verification to reject blocks whose epoch number is implausibly far from the current tip.

---

### Proof of Concept

```rust
// Pseudocode — insert N blocks with distinct parent hashes, current epoch
let pool = OrphanBlockPool::with_capacity(BLOCK_DOWNLOAD_WINDOW);
let tip_epoch = shared.snapshot().tip_header().epoch().number();
for i in 0..100_000u64 {
    let block = BlockBuilder::default()
        .parent_hash(random_byte32())          // unique unknown parent
        .epoch(EpochNumberWithFraction::new(tip_epoch, i % 1000, 1000))
        .number(tip_epoch * 1000 + i)
        // ... valid PoW, valid cellbase ...
        .build();
    pool.insert(block.into());
}
assert_eq!(pool.len(), 100_000);  // no eviction occurred
// measure RSS: grows proportionally, no bound enforced
``` [2](#0-1) [8](#0-7)

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

**File:** chain/src/utils/orphan_block_pool.rs (L159-161)
```rust
    pub fn len(&self) -> usize {
        self.inner.read().parents.len()
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
