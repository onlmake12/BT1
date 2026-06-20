### Title
Unbounded O(N²) Work in `search_orphan_leaders` via Crafted Orphan Blocks — (`chain/src/utils/orphan_block_pool.rs`, `chain/src/orphan_broker.rs`)

---

### Summary

An unprivileged remote peer can send N structurally valid blocks, each referencing a distinct unknown parent hash, causing N entries in `InnerPool.leaders`. Because `process_lonely_block` unconditionally calls `search_orphan_leaders` after every block, and `search_orphan_leaders` clones the entire leaders set and calls `get_block_status` for each leader, the k-th block triggers O(k) work. Total work across N blocks is O(N²), stalling the single-threaded `ChainService` loop and degrading sync throughput to near zero.

---

### Finding Description

**Step 1 — No hard cap on `OrphanBlockPool`.**

`OrphanBlockPool::with_capacity` is initialized with `ORPHAN_BLOCK_SIZE = BLOCK_DOWNLOAD_WINDOW as usize`: [1](#0-0) 

`with_capacity` is only a `HashMap` pre-allocation hint — there is no eviction or size enforcement anywhere in `InnerPool`. The only cleanup is `clean_expired_blocks`, which runs every 60 seconds and only removes blocks older than 6 epochs: [2](#0-1) 

**Step 2 — Each block with a unique unknown parent adds one leader.**

`InnerPool::insert` adds `parent_hash` to `leaders` whenever the parent is not already in `parents` (i.e., not in the pool): [3](#0-2) 

N blocks with N distinct unknown parent hashes → N entries in `leaders`.

**Step 3 — `search_orphan_leaders` is called unconditionally after every block.** [4](#0-3) 

**Step 4 — `search_orphan_leaders` clones all N leaders and calls `get_block_status` for each.** [5](#0-4) 

`clone_leaders` acquires a read lock and clones the entire `HashSet`: [6](#0-5) 

**Step 5 — `get_block_status` falls through to a DB lookup for unknown hashes.**

For each unknown leader hash (not in `block_status_map`, not in `header_map`), it calls `snapshot().get_block_ext(block_hash)`: [7](#0-6) 

**Step 6 — The ChainService is a single-threaded event loop.**

All block processing is sequential. The `process_block_tx` channel is bounded to 24, but the attacker can continuously refill it: [8](#0-7) 

**Step 7 — Non-contextual verify does not validate the difficulty target.**

`asynchronous_process_block` runs `non_contextual_verify` (structure + non-contextual PoW: hash ≤ claimed `compact_target`) before orphan insertion. The attacker sets `compact_target` to the maximum value, making any hash valid. Contextual difficulty validation (that `compact_target` matches the epoch's expected target) only happens in the `ConsumeUnverifiedBlocks` thread, after the block is already in the orphan pool: [9](#0-8) 

---

### Impact Explanation

The `ChainService` thread is the sole consumer of incoming blocks. With N=10,000 leaders, each new block triggers 10,000 DashMap misses + 10,000 RocksDB point-lookups in `search_orphan_leaders`. Total work across N blocks is O(N²). The thread stalls, the `process_block_tx` channel backs up, and legitimate sync blocks are not processed, degrading sync throughput to near zero.

---

### Likelihood Explanation

The attack requires only a P2P connection and the ability to craft structurally valid blocks with a self-declared low difficulty target. No hashpower, no privileged access, and no Sybil attack is needed. The attacker can sustain the attack indefinitely since the orphan pool has no eviction and the 60-second cleanup only removes blocks older than 6 epochs.

---

### Recommendation

1. **Enforce a hard cap on `OrphanBlockPool`**: Evict the oldest or a random entry when the pool exceeds a fixed limit (e.g., `BLOCK_DOWNLOAD_WINDOW`), analogous to `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` in the tx orphan pool.
2. **Bound `search_orphan_leaders` work**: Only search leaders that were affected by the current block (i.e., only check the parent hash of the newly inserted block), not all leaders.
3. **Validate `compact_target` non-contextually**: Reject blocks whose claimed difficulty is below the genesis difficulty or some minimum threshold before orphan insertion.

---

### Proof of Concept

```rust
// Pseudocode: send N blocks each with a unique random parent hash
for i in 0..N {
    let block = BlockBuilder::default()
        .parent_hash(random_byte32())   // unknown parent
        .compact_target(u32::MAX.pack()) // trivial PoW
        .number((i + 1).pack())
        .build();
    peer.send_block(block); // via P2P SendBlock message
}
// After N blocks: leaders.len() == N
// Each subsequent block triggers O(N) get_block_status calls
// Benchmark: search_orphan_leaders with N=1,100,1000,10000 and assert quadratic scaling
```

### Citations

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

**File:** chain/src/init.rs (L93-93)
```rust
    let (process_block_tx, process_block_rx) = channel::bounded(24);
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

**File:** chain/src/utils/orphan_block_pool.rs (L48-52)
```rust
        if !self.parents.contains_key(&parent_hash) {
            // Block referenced by `parent_hash` is not in the pool,
            // and it has at least one child, the new inserted block, so add it to leaders.
            self.leaders.insert(parent_hash.clone());
        }
```

**File:** chain/src/utils/orphan_block_pool.rs (L163-165)
```rust
    pub fn clone_leaders(&self) -> Vec<ParentHash> {
        self.inner.read().leaders.iter().cloned().collect()
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

**File:** chain/src/orphan_broker.rs (L107-125)
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
```

**File:** shared/src/shared.rs (L425-444)
```rust
    pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
        match self.block_status_map().get(block_hash) {
            Some(status_ref) => *status_ref.value(),
            None => {
                if self.header_map().contains_key(block_hash) {
                    BlockStatus::HEADER_VALID
                } else {
                    let verified = self
                        .snapshot()
                        .get_block_ext(block_hash)
                        .map(|block_ext| block_ext.verified);
                    match verified {
                        None => BlockStatus::UNKNOWN,
                        Some(None) => BlockStatus::BLOCK_STORED,
                        Some(Some(true)) => BlockStatus::BLOCK_VALID,
                        Some(Some(false)) => BlockStatus::BLOCK_INVALID,
                    }
                }
            }
        }
```
