Looking at the actual code, I need to verify: (1) the O(N²) call structure, (2) whether `non_contextual_verify` gates PoW before orphan insertion, and (3) whether the orphan pool has a hard size cap.

---

### Title
O(N²) Orphan Leader Scan in `search_orphan_leaders` Enables ChainService Stall — (`chain/src/orphan_broker.rs`)

### Summary

`process_lonely_block` unconditionally calls `search_orphan_leaders` after every block, which clones the entire leaders set (O(N)) and then calls `search_orphan_leader` for each entry. An attacker sending N blocks with N distinct unknown parent hashes accumulates N leaders and triggers O(N²) total work across N blocks. The `non_contextual_verify` gate does **not** check PoW, so no mining is required.

---

### Finding Description

**Call structure (confirmed in code):**

`process_lonely_block` always calls `search_orphan_leaders` at line 125, regardless of whether the block was inserted into the orphan pool or not: [1](#0-0) 

`search_orphan_leaders` clones the full leaders `HashSet` into a `Vec` (O(N) allocation + copy), then calls `search_orphan_leader` for every entry: [2](#0-1) 

`clone_leaders` is a full O(N) clone of the leaders set: [3](#0-2) 

For each leader, `search_orphan_leader` does `get_block_status` + `is_pending_verify.contains`. When the leader's parent is unknown (the attack scenario), it hits the early-return path at line 53–58 — O(1) per leader, but called N times: [4](#0-3) 

**No PoW check before orphan insertion:**

`non_contextual_verify` calls only `BlockVerifier` and `NonContextualBlockTxsVerifier`. The `BlockVerifier` explicitly lists its checks (Cellbase, BlockBytes, BlockExtension, ProposalsLimit, Duplicate, MerkleRoot) — **PoW is absent**: [5](#0-4) 

PoW lives in `PowVerifier` inside `HeaderVerifier`, which is context-dependent and runs only during full verification, long after orphan pool insertion: [6](#0-5) 

**No hard size cap on the orphan pool:**

`ORPHAN_BLOCK_SIZE` is used only as a `HashMap::with_capacity` hint, not a hard limit. `InnerPool::insert` has no size guard: [7](#0-6) [8](#0-7) 

**Entry point:**

The P2P sync path delivers blocks via `asynchronous_process_remote_block` → `asynchronous_process_lonely_block` → `ChainService::asynchronous_process_block` → `non_contextual_verify` → `orphan_broker.process_lonely_block`. No PoW check occurs anywhere in this chain before orphan insertion: [9](#0-8) 

---

### Impact Explanation

The single-threaded `ChainService` loop processes one block at a time. With N=8192 leaders, each incoming block triggers ~8192 `get_block_status` + `DashSet::contains` calls. At N=8192, each new block costs ~8192 operations; total cost for filling the pool is ~33 million operations. This serializes the entire block processing pipeline, stalling the `ChainService` thread and preventing legitimate blocks from being verified or relayed, causing a network-wide block processing stall.

---

### Likelihood Explanation

Any peer can send `SendBlock` P2P messages. Blocks need only pass `non_contextual_verify` (structural checks, no PoW). An attacker constructs N minimal-valid blocks each referencing a distinct fabricated parent hash. No hashpower is required. The attack is repeatable and cheap.

---

### Recommendation

1. **Bound `search_orphan_leaders` invocation**: Only call it when a block is actually inserted into the orphan pool (i.e., move the call inside the `else` branch at line 122), not unconditionally.
2. **Enforce a hard cap** in `InnerPool::insert`: evict the oldest leader when `leaders.len()` exceeds `ORPHAN_BLOCK_SIZE`.
3. **Rate-limit orphan insertions per peer**: track how many orphans each peer has contributed and disconnect peers that exceed a threshold.
4. **Lazy leader scan**: instead of scanning all leaders on every insertion, only check the newly inserted block's parent hash.

---

### Proof of Concept

```rust
// Pseudocode: attacker sends 8192 blocks, each with a unique fake parent hash
for i in 0..8192 {
    let fake_parent = Byte32::from([i as u8; 32]); // distinct unknown parent
    let block = build_minimal_valid_block(fake_parent); // passes non_contextual_verify, no PoW
    peer.send_block(block); // P2P SendBlock message
}
// After 8192 blocks: each new block triggers clone_leaders() returning 8192 entries
// + 8192 × (get_block_status + is_pending_verify.contains)
// ChainService thread is saturated; legitimate blocks queue indefinitely
```

Benchmark: call `search_orphan_leaders` with a pool containing 8192 leaders (all with unknown-parent status); assert per-call latency stays below the CKB block interval (~8 s). With the current code it will not.

### Citations

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

**File:** chain/src/orphan_broker.rs (L121-125)
```rust
        } else {
            self.orphan_blocks_broker.insert(lonely_block);
        }

        self.search_orphan_leaders();
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

**File:** chain/src/utils/orphan_block_pool.rs (L163-165)
```rust
    pub fn clone_leaders(&self) -> Vec<ParentHash> {
        self.inner.read().leaders.iter().cloned().collect()
    }
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

**File:** verification/src/header_verifier.rs (L33-34)
```rust
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
```

**File:** chain/src/init.rs (L22-22)
```rust
const ORPHAN_BLOCK_SIZE: usize = BLOCK_DOWNLOAD_WINDOW as usize;
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
