All code claims verified against the actual source. Every cited line matches the repository exactly. Here is the audit result:

---

Audit Report

## Title
Unbounded O(N) Orphan Leader Scan Per Block Without PoW Guard Degrades ChainService Throughput — (`chain/src/orphan_broker.rs`, `chain/src/utils/orphan_block_pool.rs`)

## Summary
`process_lonely_block` unconditionally calls `search_orphan_leaders()` after every block, which clones and iterates the entire leaders set. Because `non_contextual_verify` does not check PoW, any P2P peer can cheaply inject structurally valid blocks with distinct unknown parent hashes, each becoming a new orphan leader. With N accumulated leaders, every block processed — including legitimate chain-tip blocks — incurs O(N) `get_block_status` and `is_pending_verify.contains` calls on the single-threaded `ChainService`, degrading throughput proportionally to orphan pool size.

## Finding Description

**Root cause 1 — Unconditional O(N) scan.**
`process_lonely_block` calls `search_orphan_leaders()` at line 125 unconditionally, outside all conditional branches, regardless of whether the incoming block was itself orphaned: [1](#0-0) 

`search_orphan_leaders` iterates every leader by cloning the full `HashSet<ParentHash>`: [2](#0-1) 

`clone_leaders` acquires a read lock and clones the entire set — O(N) in leader count: [3](#0-2) 

For each leader, `search_orphan_leader` performs a `get_block_status` lookup and an `is_pending_verify.contains` check. For fake leaders (unknown parent hash, not stored, not pending), both checks are performed before an early return — O(N) total per block: [4](#0-3) 

**Root cause 2 — No PoW check in `non_contextual_verify`.**
`asynchronous_process_block` calls `non_contextual_verify` before orphan insertion: [5](#0-4) 

`BlockVerifier::verify` only checks proposals limit, block bytes, cellbase structure, duplicates, and merkle root — no PoW: [6](#0-5) 

PoW is only verified in `HeaderVerifier`, which requires the parent header (context-dependent, never reached for orphans): [7](#0-6) 

An attacker can craft structurally valid blocks (valid cellbase, correct merkle root, arbitrary parent hash, any nonce) at negligible CPU cost.

**Root cause 3 — No capacity enforcement on the orphan pool.**
`OrphanBlockPool::with_capacity` only pre-allocates the `HashMap`; `insert` never rejects a block regardless of pool size: [8](#0-7) 

`ORPHAN_BLOCK_SIZE` = `BLOCK_DOWNLOAD_WINDOW` = 8192 is only a pre-allocation hint, not a hard limit: [9](#0-8) 

**Root cause 4 — Cleanup does not bound the attack.**
`clean_expired_orphans` runs every 60 seconds and only removes blocks from epochs older than `tip_epoch - 6`. An attacker using current-epoch blocks is never cleaned up: [10](#0-9) [11](#0-10) 

## Impact Explanation
The `ChainService` thread is single-threaded and processes blocks sequentially. With N orphan leaders, every block received — including legitimate chain-tip blocks — triggers N `get_block_status` + N `is_pending_verify.contains` calls before the block can be forwarded for verification. Block processing throughput degrades linearly with N. Since the pool is unbounded, N is not capped at 8192. At large N, the per-block overhead becomes a sustained bottleneck, delaying block propagation and verification across the node. An attacker targeting multiple nodes simultaneously can cause measurable CKB network congestion at very low cost (no PoW required).

**Impact: High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The attack requires only a P2P connection and the ability to send `SendBlock` messages. Constructing a valid block requires: one cellbase transaction, a correct merkle root over it, and an arbitrary parent hash. No PoW is required. The attacker can accumulate leaders gradually, pacing sends to avoid the bounded `process_block_tx` channel (capacity 24). The attack is concretely reachable from any unprivileged peer and is repeatable indefinitely since cleanup only removes old-epoch blocks.

## Recommendation
1. **Enforce a hard capacity limit in `OrphanBlockPool::insert`**: reject insertion when `leaders.len()` exceeds a threshold (e.g., 256 distinct leaders), evicting the oldest leader if desired.
2. **Scope `search_orphan_leaders` to the newly processed block**: instead of scanning all leaders on every block, only check whether the current block's hash resolves any pending orphan chain (i.e., check if the current block's hash is a known leader).
3. **Add PoW pre-screening** at the P2P ingress layer before blocks enter `asynchronous_process_block`, or at minimum before orphan pool insertion, to raise the cost of injecting fake leaders.

## Proof of Concept
```
1. Connect to a CKB node as a P2P peer.
2. Construct 10,000 minimal blocks, each with:
   - A valid cellbase transaction (correct structure, block number in input since field)
   - A correct merkle root computed over the cellbase
   - A unique random parent_hash not present in the node's DB
   - Any nonce (PoW not checked by non_contextual_verify)
3. Send all 10,000 via SendBlock P2P messages, pacing to avoid channel backpressure.
   Each passes non_contextual_verify, is written to DB, and inserted into the orphan
   pool as a new leader (distinct parent_hash → distinct leader entry).
4. Send a legitimate block (or any valid block).
5. Observe: process_lonely_block → search_orphan_leaders iterates all 10,000 leaders,
   performing 10,000 get_block_status + 10,000 is_pending_verify.contains calls.
6. Measure per-block processing latency at baseline (0 leaders) vs. after accumulation
   (10,000 leaders). Assert latency scales linearly with leader count.
7. Repeat to grow the pool further; confirm cleanup does not remove current-epoch leaders.
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
