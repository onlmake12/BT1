### Title
O(N²) CPU Exhaustion via Orphan Block Leader Scan Without PoW Requirement — (`chain/src/orphan_broker.rs`, `chain/src/utils/orphan_block_pool.rs`)

---

### Summary

`search_orphan_leaders` is called unconditionally after every `process_lonely_block`. It iterates the entire `leaders` set and performs a `get_block_status` lookup per entry. Because the non-contextual `BlockVerifier` does **not** check Proof-of-Work, an unprivileged remote peer can inject N structurally valid blocks with distinct unknown parent hashes, growing the leaders set to N entries and causing N·(N+1)/2 total status lookups across N insertions — O(N²) work on the single-threaded `ChainService` loop.

---

### Finding Description

**Step 1 — Entry point: no PoW gate in non-contextual verification**

`BlockVerifier::verify` runs only these checks: [1](#0-0) 

`BlockProposalsLimitVerifier`, `BlockBytesVerifier`, `CellbaseVerifier`, `DuplicateVerifier`, `MerkleRootVerifier` — **no `PowVerifier`**. PoW is only enforced in the contextual `HeaderVerifier`, which is never reached for orphan blocks. An attacker therefore needs only to craft a structurally valid block (valid cellbase, correct merkle root, within size limits) with an arbitrary unknown parent hash.

**Step 2 — Block reaches the orphan pool**

`asynchronous_process_block` calls `non_contextual_verify`, then `insert_block` (DB write), then `orphan_broker.process_lonely_block`: [2](#0-1) 

Inside `process_lonely_block`, when the parent is unknown (not stored, not invalid, not pending), the block is inserted into the orphan pool: [3](#0-2) 

**Step 3 — `search_orphan_leaders` is called unconditionally after every insertion** [4](#0-3) 

`search_orphan_leaders` clones the entire leaders set and calls `search_orphan_leader` for each entry: [5](#0-4) 

**Step 4 — Each leader with an unknown parent causes a status lookup then early-returns** [6](#0-5) 

For an unknown parent: `get_block_status` returns neither `BLOCK_INVALID` nor `BLOCK_STORED`, and `is_pending_verify` is false, so the function returns at line 58 — but only after performing both lookups.

**Step 5 — Leaders set grows unboundedly**

`InnerPool::insert` adds each new unknown parent to `leaders`: [7](#0-6) 

`OrphanBlockPool::with_capacity` is only an initial HashMap allocation hint, not a hard cap: [8](#0-7) 

`ORPHAN_BLOCK_SIZE = BLOCK_DOWNLOAD_WINDOW as usize` sets the initial capacity, but the pool grows without bound. [9](#0-8) 

**Step 6 — Single-threaded loop is blocked**

The entire call chain runs synchronously inside the `ChainService` event loop: [10](#0-9) 

No other block can be processed while `search_orphan_leaders` is executing.

---

### Impact Explanation

After N orphan insertions with distinct unknown parents, the leaders set has N entries. The k-th insertion triggers a scan of k leaders. Total work: 1 + 2 + … + N = N(N+1)/2 hash-map lookups. At N = 10,000 that is ~50 million lookups on the single-threaded `ChainService`, stalling block processing for seconds. The `clean_expired_orphan_timer` fires only every 60 seconds, giving the attacker a large window.

---

### Likelihood Explanation

The attack requires no mining power — only structurally valid blocks (valid cellbase, merkle root, within byte limits) with arbitrary parent hashes. A single peer can send thousands of such blocks rapidly. The channel into `ChainService` is bounded at 24 messages, but the attacker can sustain the flood continuously, keeping the leaders set large and the per-block scan cost high.

---

### Recommendation

1. **Enforce a hard cap on the orphan pool** — reject `insert` when `leaders.len()` or `parents.len()` exceeds `ORPHAN_BLOCK_SIZE`, evicting the oldest entry.
2. **Scan only the newly relevant leader** — instead of scanning all leaders after every insertion, `process_lonely_block` should only call `search_orphan_leader` for the parent hash of the just-inserted block, not the entire set.
3. **Per-peer orphan rate limiting** — track how many orphans each peer has contributed and ban peers that exceed a threshold.
4. **Add PoW check to non-contextual verification** — or at the sync-layer entry point before blocks are written to the DB and inserted into the orphan pool.

---

### Proof of Concept

```rust
// Pseudocode: attacker sends N blocks with unique unknown parent hashes
for i in 0..N {
    let block = build_valid_block_no_pow(
        parent_hash = random_unknown_hash(i),  // unique per block
    );
    peer.send_block(block);
    // ChainService: non_contextual_verify (no PoW check) -> insert_block -> process_lonely_block
    //   -> orphan_blocks_broker.insert (leaders grows to i+1)
    //   -> search_orphan_leaders (iterates i+1 leaders, each does get_block_status -> early return)
    // Total status lookups after N blocks: N*(N+1)/2
}
// Benchmark: process_lonely_block latency at N=1000 vs N=10000 should show super-linear growth
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

**File:** chain/src/chain_service.rs (L43-55)
```rust
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

**File:** chain/src/orphan_broker.rs (L39-59)
```rust
    fn search_orphan_leader(&self, leader_hash: ParentHash) {
        let leader_status = self.shared.get_block_status(&leader_hash);

        if leader_status.eq(&BlockStatus::BLOCK_INVALID) {
            let descendants: Vec<LonelyBlockHash> = self
                .orphan_blocks_broker
                .remove_blocks_by_parent(&leader_hash);
            for descendant in descendants {
                self.process_invalid_block(descendant);
            }
            return;
        }

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

**File:** chain/src/utils/orphan_block_pool.rs (L48-52)
```rust
        if !self.parents.contains_key(&parent_hash) {
            // Block referenced by `parent_hash` is not in the pool,
            // and it has at least one child, the new inserted block, so add it to leaders.
            self.leaders.insert(parent_hash.clone());
        }
```

**File:** chain/src/utils/orphan_block_pool.rs (L134-138)
```rust
    pub fn with_capacity(capacity: usize) -> Self {
        OrphanBlockPool {
            inner: RwLock::new(InnerPool::with_capacity(capacity)),
        }
    }
```

**File:** chain/src/init.rs (L22-22)
```rust
const ORPHAN_BLOCK_SIZE: usize = BLOCK_DOWNLOAD_WINDOW as usize;
```
