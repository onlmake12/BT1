### Title
Orphan Pool Cleanup Bypass via Single-Block Epoch Spoofing — (`chain/src/utils/orphan_block_pool.rs`)

### Summary

`InnerPool::need_clean` samples only the first entry of a per-parent `HashMap` to decide whether an entire sibling group should be evicted. Because `HashMap` iteration order is non-deterministic and the non-contextual block admission path performs **no PoW check and no epoch validation**, a remote peer can insert structurally valid blocks with an arbitrarily high `epoch_number` field, ensuring `need_clean` always returns `false` for the targeted group and preventing `clean_expired_blocks` from ever evicting those orphans.

---

### Finding Description

**Root cause 1 — `need_clean` samples only one block per group:** [1](#0-0) 

`map.iter().next()` returns an arbitrary entry from the inner `HashMap<Byte32, LonelyBlockHash>`. If that entry's `epoch_number + EXPIRED_EPOCH >= tip_epoch`, the function returns `false` and the **entire sibling group** — including blocks with epoch 0 — is kept alive.

**Root cause 2 — `epoch_number` is taken verbatim from the block header:** [2](#0-1) 

No range or continuity check is applied at this point.

**Root cause 3 — Non-contextual verification does not check PoW or epoch:** [3](#0-2) 

`BlockVerifier` only runs `CellbaseVerifier`, `BlockBytesVerifier`, `BlockProposalsLimitVerifier`, `DuplicateVerifier`, and `MerkleRootVerifier`: [4](#0-3) 

PoW is only checked inside the contextual `HeaderVerifier`: [5](#0-4) 

That verifier is **never called** on the non-contextual path that feeds the orphan pool. An attacker can therefore craft a block with any `epoch_number` value, satisfy the structural checks (valid cellbase `since`, correct merkle root, etc.), and have it accepted into the orphan pool without solving PoW.

**Root cause 4 — Cleanup iterates leaders but delegates the expiry decision to `need_clean`:** [6](#0-5) 

If `need_clean` returns `false` for a leader, `remove_blocks_by_parent` is never called and all descendants under that leader remain in the pool indefinitely.

---

### Impact Explanation

An attacker who continuously sends structurally valid blocks (no PoW required) with the same unknown parent hash and `epoch_number = tip_epoch - 1` will:

1. Accumulate entries in `blocks[parent_hash]` indefinitely.
2. Ensure `need_clean` always returns `false` for that group (since `(tip_epoch-1) + 6 >= tip_epoch`).
3. As `tip_epoch` advances, simply refresh with new blocks carrying the updated `tip_epoch - 1`.

The orphan pool (`blocks`, `parents`, `leaders`) grows without bound, consuming heap memory until the node OOMs or becomes unresponsive — a complete denial of service against any node reachable over P2P.

---

### Likelihood Explanation

- No PoW is required; only a structurally valid block (correct cellbase, merkle root).
- The P2P `SendBlock` message is the standard, unauthenticated entry point.
- The attack is cheap to sustain: one new block per epoch advancement per targeted parent group.
- No special peer privileges, no key material, no majority hashpower.

---

### Recommendation

1. **Fix `need_clean`**: Check **all** blocks in the group, or use the **maximum** epoch among siblings (so a group is only retained if at least one block is genuinely recent):
   ```rust
   fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
       self.blocks
           .get(parent_hash)
           .map(|map| {
               map.values().all(|b| b.epoch_number() + EXPIRED_EPOCH < tip_epoch)
           })
           .unwrap_or_default()
   }
   ```
2. **Add epoch plausibility check** at non-contextual admission: reject blocks whose `epoch_number` exceeds `tip_epoch + EXPIRED_EPOCH` or is otherwise implausible relative to the block number.
3. **Cap orphan pool size** with a hard limit and evict by insertion order (LRU or FIFO) when the cap is reached, independent of epoch checks.

---

### Proof of Concept

```rust
// Build an InnerPool with:
//   - 100 blocks under parent_hash P, all with epoch=0 (old)
//   - 1 block under parent_hash P with epoch=tip_epoch-1 (recent)
// Call clean_expired_blocks(tip_epoch=20).
// Assert: pool still contains all 101 blocks (not cleaned).
//
// This works because map.iter().next() may return the recent-epoch block,
// causing need_clean to return false for the entire group.
// To guarantee the outcome, insert ONLY blocks with epoch=tip_epoch-1
// under the target parent hash — need_clean always returns false.
```

The existing test `test_remove_expired_blocks` only tests the case where **all** blocks in a group share the same (old) epoch, so it does not catch this bypass. [7](#0-6)

### Citations

**File:** chain/src/utils/orphan_block_pool.rs (L99-110)
```rust
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

**File:** chain/src/lib.rs (L97-98)
```rust
        let epoch_number: EpochNumber = block.epoch().number();

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

**File:** verification/src/block_verifier.rs (L36-48)
```rust
impl<'a> Verifier for BlockVerifier<'a> {
    type Target = BlockView;

    fn verify(&self, target: &BlockView) -> Result<(), Error> {
        let max_block_proposals_limit = self.consensus.max_block_proposals_limit();
        let max_block_bytes = self.consensus.max_block_bytes();
        BlockProposalsLimitVerifier::new(max_block_proposals_limit).verify(target)?;
        BlockBytesVerifier::new(max_block_bytes).verify(target)?;
        CellbaseVerifier::new().verify(target)?;
        DuplicateVerifier::new().verify(target)?;
        MerkleRootVerifier::new().verify(target)
    }
}
```

**File:** verification/src/header_verifier.rs (L32-34)
```rust
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
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
