### Title
O(n) Orphan Leader Scan Per Block Without PoW Guard Degrades ChainService Throughput — (`chain/src/orphan_broker.rs`, `chain/src/utils/orphan_block_pool.rs`)

---

### Summary

`process_lonely_block` unconditionally calls `search_orphan_leaders()` after every block, which iterates the entire leaders set via `clone_leaders()`. Because `non_contextual_verify` does **not** check PoW, an unprivileged remote peer can cheaply inject structurally valid blocks with distinct unknown parent hashes, each becoming a new orphan leader. With N leaders accumulated, every subsequent block processed costs O(N) work on the single-threaded `ChainService`, degrading block processing throughput proportionally to orphan pool size.

---

### Finding Description

**Step 1 — The unconditional O(n) scan.**

`process_lonely_block` always calls `search_orphan_leaders()` at line 125, regardless of whether the incoming block was itself orphaned: [1](#0-0) 

`search_orphan_leaders` iterates every leader via `clone_leaders()`: [2](#0-1) 

`clone_leaders()` acquires a read lock and clones the entire `HashSet<ParentHash>` — O(n) in the number of leaders: [3](#0-2) 

For each leader, `search_orphan_leader` performs a `get_block_status` lookup and an `is_pending_verify.contains` check — both O(1) individually, but O(n) total per block processed. [4](#0-3) 

**Step 2 — No PoW check in `non_contextual_verify`.**

`asynchronous_process_block` calls `non_contextual_verify` before inserting into the orphan pool: [5](#0-4) 

`BlockVerifier::verify` only checks proposals limit, block bytes, cellbase structure, duplicates, and merkle root — **no PoW**: [6](#0-5) 

PoW is only verified in `HeaderVerifier` (context-dependent, requires parent header): [7](#0-6) 

This means an attacker can craft structurally valid blocks with arbitrary unknown parent hashes at negligible CPU cost, bypassing the only meaningful admission filter.

**Step 3 — No capacity enforcement on the orphan pool.**

`OrphanBlockPool::with_capacity` only pre-allocates the `HashMap`; it does not enforce a hard limit on insertions. `insert` never rejects a block: [8](#0-7) 

`ORPHAN_BLOCK_SIZE` = `BLOCK_DOWNLOAD_WINDOW` = 8192 is only a hint: [9](#0-8) 

**Step 4 — Cleanup does not bound the attack.**

`clean_expired_orphans` runs every 60 seconds and only removes blocks from epochs older than `tip_epoch - 6`. An attacker using current-epoch blocks is never cleaned up: [10](#0-9) [11](#0-10) 

---

### Impact Explanation

The `ChainService` thread is single-threaded and processes blocks sequentially. With N orphan leaders, every block received — including legitimate chain-tip blocks — triggers N `get_block_status` + N `is_pending_verify.contains` calls. Block processing throughput degrades linearly with N. At N = 8192 (the nominal pool size), the overhead per block is measurable; at larger N (pool is unbounded), it becomes a sustained bottleneck. This delays block propagation and verification, degrading node liveness.

---

### Likelihood Explanation

The attack requires only a P2P connection and the ability to send `SendBlock` messages containing structurally valid blocks (valid cellbase, correct merkle root, arbitrary parent hash). No PoW is required. The attacker can accumulate leaders gradually over time. The `process_block_tx` channel is bounded at 24, but the attacker simply paces sends to avoid backpressure. This is concretely reachable from any unprivileged peer.

---

### Recommendation

1. **Enforce a hard capacity limit in `OrphanBlockPool::insert`**: reject or evict when `leaders.len()` exceeds a threshold (e.g., 256 distinct leaders).
2. **Scope `search_orphan_leaders` to the newly inserted block's parent**: instead of scanning all leaders on every block, only check whether the current block's parent (or the current block's hash) resolves any pending orphan chain.
3. **Add PoW pre-screening** at the P2P layer before blocks enter `asynchronous_process_block`, or at minimum before orphan insertion.

---

### Proof of Concept

```
1. Connect to a CKB node as a peer.
2. Construct 2000 blocks, each with:
   - A valid cellbase transaction
   - A correct merkle root
   - A unique random parent_hash not in the node's DB
   - Any nonce (PoW not checked by non_contextual_verify)
3. Send all 2000 via SendBlock P2P messages.
   Each passes non_contextual_verify, is inserted into the orphan pool as a new leader.
4. Now send a legitimate block (or any valid block).
5. Observe: process_lonely_block → search_orphan_leaders iterates all 2000 leaders,
   performing 2000 get_block_status + 2000 is_pending_verify.contains calls.
6. Measure per-block processing latency before (baseline) and after (2000 leaders).
   Assert latency scales linearly with leader count.
```

### Citations

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

**File:** chain/src/utils/orphan_block_pool.rs (L163-165)
```rust
    pub fn clone_leaders(&self) -> Vec<ParentHash> {
        self.inner.read().leaders.iter().cloned().collect()
    }
```

**File:** chain/src/chain_service.rs (L61-63)
```rust
                recv(clean_expired_orphan_timer) -> _ => {
                    self.orphan_broker.clean_expired_orphans();
                },
```

**File:** chain/src/chain_service.rs (L117-131)
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

**File:** verification/src/header_verifier.rs (L32-34)
```rust
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
```

**File:** chain/src/init.rs (L22-22)
```rust
const ORPHAN_BLOCK_SIZE: usize = BLOCK_DOWNLOAD_WINDOW as usize;
```
