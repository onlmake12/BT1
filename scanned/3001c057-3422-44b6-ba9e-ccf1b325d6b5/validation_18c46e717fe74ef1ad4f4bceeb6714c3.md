Based on my analysis of the production code, this is a valid vulnerability.

---

### Title
Unbounded `OrphanBlockPool` with O(N²) `search_orphan_leaders` scan — (`chain/src/utils/orphan_block_pool.rs`, `chain/src/orphan_broker.rs`, `chain/src/chain_service.rs`)

### Summary

An unprivileged remote peer can flood the node with syntactically valid blocks referencing unique unknown parent hashes. Because `BlockVerifier` (the non-contextual gate) does **not** check PoW, and `InnerPool::insert` has no size cap, each block is written to `ChainDB` and inserted into `OrphanBlockPool`. The `leaders` `HashSet` grows to O(N), and `search_orphan_leaders` — called on **every** block arrival — iterates all N leaders, producing O(N²) total CPU cost and unbounded memory/disk growth.

### Finding Description

**Gate 1 — `non_contextual_verify` skips PoW.**

`BlockVerifier::verify` runs only five checks: [1](#0-0) 

None of them is a PoW check. PoW lives in `HeaderVerifier`, which is contextual (requires the parent header) and is never invoked for orphan blocks. An attacker can set `compact_target` to minimum difficulty (e.g., `0x207fffff`) so that any nonce satisfies `EaglesongPowEngine::verify`, or simply omit valid PoW entirely — the non-contextual path will not reject the block.

**Gate 2 — `InnerPool::insert` has no size cap.**

`with_capacity` only pre-allocates the `HashMap`; there is no maximum enforced at insertion time: [2](#0-1) 

Every block with a unique unknown parent hash adds one entry to `leaders`.

**Gate 3 — Block is written to `ChainDB` before orphan pool insertion.** [3](#0-2) 

Disk is consumed for every accepted orphan block.

**Gate 4 — `search_orphan_leaders` is O(N) per block arrival.**

`process_lonely_block` unconditionally calls `search_orphan_leaders` after every block: [4](#0-3) 

`search_orphan_leaders` clones and iterates the entire `leaders` set: [5](#0-4) 

With N orphan leaders, each new block arrival costs O(N) work, giving O(N²) total cost for N flood blocks.

**Mitigation analysis — `clean_expired_orphans` is insufficient.**

The cleanup timer fires every 60 seconds and removes only blocks where `epoch_number + EXPIRED_EPOCH < tip_epoch` (EXPIRED_EPOCH = 6): [6](#0-5) 

An attacker sets the epoch field of crafted blocks to the current epoch, so no cleanup occurs for ~24 hours (6 epochs × ~4 h/epoch on mainnet). During that window the pool is unbounded.

### Impact Explanation

- **Memory exhaustion**: `leaders` `HashSet`, `blocks` `HashMap`, and `parents` `HashMap` all grow without bound.
- **Disk exhaustion**: every orphan block is committed to `ChainDB` via `db_txn.insert_block` before orphan pool insertion.
- **O(N²) CPU**: `search_orphan_leaders` acquires a write lock on `InnerPool` and iterates all N leaders on every block arrival; block processing halts as the scan dominates.

### Likelihood Explanation

The attack requires only a P2P connection and the ability to send `SendBlock` messages. No PoW computation is needed (compact_target is attacker-controlled and not validated non-contextually). A single peer sending 100,000 blocks with unique parent hashes is sufficient to demonstrate the effect.

### Recommendation

1. **Enforce a hard cap** in `InnerPool::insert` (e.g., reject insertion when `parents.len() >= MAX_ORPHAN_POOL_SIZE`).
2. **Add a PoW check to the non-contextual path**: even without parent context, `EaglesongPowEngine::verify` can validate that the block hash meets the block's own claimed `compact_target`. This raises the cost of flooding to real mining work.
3. **Bound `leaders` separately**: cap the number of distinct leader hashes accepted per peer per time window.
4. **Accelerate expiry**: reduce `EXPIRED_EPOCH` or add a wall-clock TTL independent of epoch progression.

### Proof of Concept

```rust
// Pseudocode: flood node with orphan blocks
for i in 0..100_000 {
    let fake_parent = unique_hash(i);          // unknown parent
    let block = craft_block(
        parent_hash = fake_parent,
        compact_target = 0x207fffff,           // trivially easy PoW
        epoch = current_epoch,                 // avoids clean_expired_orphans
    );
    // block passes BlockVerifier + NonContextualBlockTxsVerifier
    // block is written to ChainDB
    // block is inserted into OrphanBlockPool (leaders grows by 1)
    // search_orphan_leaders iterates i leaders — O(i) work
    peer.send_block(block);
}
// Total CPU: O(N^2); memory: O(N); disk: O(N * block_size)
``` [7](#0-6) [8](#0-7)

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
