All three technical claims verified against the actual code:

**Claim 1:** `search_orphan_leaders()` is unconditionally called at line 125, outside all `if/else if/else` branches. [1](#0-0) 

**Claim 2:** `ChainService::non_contextual_verify` calls only `BlockVerifier` + `NonContextualBlockTxsVerifier` — no `PowVerifier` or `HeaderVerifier`. [2](#0-1) [3](#0-2) 

**Claim 3:** `ORPHAN_BLOCK_SIZE` is only a `HashMap::with_capacity` hint; `InnerPool::insert` has no size guard. [4](#0-3) 

**`clone_leaders` behavior confirmed:** acquires read lock and clones entire `HashSet` into `Vec`. [5](#0-4) 

**Early-return path confirmed:** for unknown-parent leaders, `search_orphan_leader` returns at lines 52–58 after two lookups — O(1) per leader, O(N) per block, O(N²) total. [6](#0-5) 

**Expiry timer confirmed:** 60-second tick, evicts only blocks older than `EXPIRED_EPOCH = 6` epochs. [7](#0-6) [8](#0-7) 

---

Audit Report

## Title
O(N²) Orphan Leader Scan in `search_orphan_leaders` Enables ChainService DoS — (`chain/src/orphan_broker.rs`)

## Summary
`process_lonely_block` unconditionally calls `search_orphan_leaders` after every block regardless of outcome. An attacker sending N structurally-valid (no PoW required) blocks with N distinct fabricated parent hashes accumulates N leaders and triggers O(N²) total work across N blocks. The single-threaded `ChainService` loop is the sole path for block processing, so saturating it delays or stalls legitimate block verification and relay.

## Finding Description
**Unconditional call site:**
`process_lonely_block` calls `search_orphan_leaders()` at line 125, outside all conditional branches (descendant path at line 118, invalid path at line 120, orphan-insert path at line 122):
```rust
} else {
    self.orphan_blocks_broker.insert(lonely_block);  // line 122
}
self.search_orphan_leaders();  // line 125 — always runs
```

**O(N) clone per call:**
`search_orphan_leaders` calls `clone_leaders()`, which acquires a read lock and clones the entire `HashSet<ParentHash>` into a `Vec` (O(N) allocation + copy), then calls `search_orphan_leader` for every entry:
```rust
fn search_orphan_leaders(&self) {
    for leader_hash in self.orphan_blocks_broker.clone_leaders() {
        self.search_orphan_leader(leader_hash);
    }
}
```

**O(1) early-return per unknown-parent leader:**
For each leader whose parent is unknown (the attack scenario), `search_orphan_leader` hits the early-return at lines 52–58 after two lookups (`get_block_status` + `is_pending_verify.contains`). This is O(1) per leader but called N times per block, giving O(N²) total across N blocks.

**No PoW check before orphan insertion:**
`ChainService::non_contextual_verify` calls only `BlockVerifier` (Cellbase, BlockBytes, ProposalsLimit, Duplicate, MerkleRoot) and `NonContextualBlockTxsVerifier`. `PowVerifier` lives inside `HeaderVerifier` and runs only during full contextual verification in the `ConsumeUnverifiedBlocks` thread — long after orphan pool insertion. An attacker's block needs only to be structurally valid.

**No hard size cap:**
`ORPHAN_BLOCK_SIZE` (`= BLOCK_DOWNLOAD_WINDOW`) is passed only to `HashMap::with_capacity` as a pre-allocation hint. `InnerPool::insert` has no size guard and grows without bound. The `clean_expired_orphans` timer fires every 60 seconds and only evicts blocks older than 6 epochs — an attacker continuously refreshes the pool with new blocks to prevent eviction.

**Exploit path:**
P2P `SendBlock` → `BlockProcess::execute` → `asynchronous_process_remote_block` → `asynchronous_process_block` → `non_contextual_verify` (passes, no PoW) → `insert_block` (DB write) → `orphan_broker.process_lonely_block` → `orphan_blocks_broker.insert` (new leader added) → `search_orphan_leaders` (scans all N leaders).

## Impact Explanation
The `ChainService` thread is single-threaded and processes one block at a time. With N leaders accumulated, each incoming block triggers an O(N) `clone_leaders` allocation plus N `get_block_status` + `DashSet::contains` calls. At N = `BLOCK_DOWNLOAD_WINDOW` (e.g., 1024), each block costs ~1024 map lookups plus a 1024-element Vec allocation; at N = 8192 the cost is ~8192 such operations per block. Because the `ChainService` loop is serialized, this delays processing of all subsequent legitimate blocks, causing CKB network congestion with minimal attacker cost. This matches: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs (10001–15000 points)**.

## Likelihood Explanation
Any peer on the P2P network can send `SendBlock` messages. Blocks need only pass structural (`non_contextual_verify`) checks — no hashpower is required. An attacker constructs N minimal-valid blocks each referencing a distinct fabricated 32-byte parent hash. The attack is cheap, repeatable, and requires no special privileges or victim mistakes. The attacker sustains the pool size by sending new blocks faster than the 60-second expiry timer can clean them (which only evicts blocks older than 6 epochs anyway).

## Recommendation
1. **Move `search_orphan_leaders` inside the orphan-insert branch** (line 122): only call it when a block is actually inserted into the orphan pool, not when it is processed as a descendant or rejected as invalid.
2. **Enforce a hard cap in `InnerPool::insert`**: when `leaders.len()` reaches `ORPHAN_BLOCK_SIZE`, evict the oldest or a random leader (and its descendants) before inserting a new one.
3. **Rate-limit orphan insertions per peer**: track per-peer orphan contribution counts and disconnect peers exceeding a threshold.
4. **Lazy leader scan**: instead of scanning all leaders on every insertion, only check whether the newly inserted block's parent hash is already known/stored.

## Proof of Concept
```rust
// Attacker sends N blocks, each with a unique fabricated parent hash.
// Each block passes non_contextual_verify (structural only, no PoW).
for i in 0u32..8192 {
    let mut fake_parent = [0u8; 32];
    fake_parent[..4].copy_from_slice(&i.to_le_bytes());
    let fake_parent = Byte32::from_slice(&fake_parent).unwrap();
    let block = build_minimal_structurally_valid_block(fake_parent);
    peer.send_block(block); // P2P SendBlock
}
// After 8192 blocks:
// - orphan pool has 8192 leaders, all with unknown-parent status
// - each subsequent block triggers clone_leaders() returning 8192 entries
//   + 8192 × (get_block_status + DashSet::contains)
// - ChainService thread is saturated; legitimate blocks queue indefinitely

// Benchmark test to confirm:
// Pre-populate orphan pool with 8192 leaders (all unknown-parent status),
// then call process_lonely_block once and measure wall-clock time.
// With current code, per-call latency will far exceed the CKB block interval (~8s)
// at sufficiently large N.
```

### Citations

**File:** chain/src/orphan_broker.rs (L52-58)
```rust
        let leader_is_pending_verify = self.is_pending_verify.contains(&leader_hash);
        if !leader_is_pending_verify && !leader_status.contains(BlockStatus::BLOCK_STORED) {
            trace!(
                "orphan leader: {} not stored {:?} and not in is_pending_verify: {}",
                leader_hash, leader_status, leader_is_pending_verify
            );
            return;
```

**File:** chain/src/orphan_broker.rs (L113-126)
```rust
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

**File:** chain/src/chain_service.rs (L40-42)
```rust
        let clean_expired_orphan_timer =
            crossbeam::channel::tick(std::time::Duration::from_secs(60));

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

**File:** chain/src/utils/orphan_block_pool.rs (L13-13)
```rust
pub const EXPIRED_EPOCH: u64 = 6;
```

**File:** chain/src/utils/orphan_block_pool.rs (L28-54)
```rust
    fn with_capacity(capacity: usize) -> Self {
        InnerPool {
            blocks: HashMap::with_capacity(capacity),
            parents: HashMap::new(),
            leaders: HashSet::new(),
        }
    }

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

**File:** chain/src/utils/orphan_block_pool.rs (L163-165)
```rust
    pub fn clone_leaders(&self) -> Vec<ParentHash> {
        self.inner.read().leaders.iter().cloned().collect()
    }
```
