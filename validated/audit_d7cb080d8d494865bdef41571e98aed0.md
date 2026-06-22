### Title
Orphan Block Pool Eviction Bypass via Epoch-Shielded Subtrees — (`chain/src/utils/orphan_block_pool.rs`)

### Summary

`InnerPool::need_clean` samples only the **first direct child** of a leader to decide whether to evict an entire subtree. An attacker can insert a crafted "shield" block (direct child of a leader, with a recent epoch) followed by expired-epoch descendants. Because `need_clean` returns `false` for the shield block, `clean_expired_blocks` skips the whole subtree, leaving expired deep descendants permanently in the pool.

### Finding Description

`clean_expired_blocks` iterates over every leader and calls `need_clean`: [1](#0-0) 

`need_clean` takes only the **first** entry from the leader's child map: [2](#0-1) 

The function checks `lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch` for that single block. If it returns `false`, the entire subtree — including arbitrarily deep descendants with epoch 0 — is never passed to `remove_blocks_by_parent`.

The non-contextual block verifier used as the gatekeeper before orphan-pool insertion is `BlockVerifier`, which checks only proposals limit, block bytes, cellbase structure, duplicate transactions, and merkle root: [3](#0-2) 

It does **not** check epoch ordering between a block and its parent. The contextual `EpochVerifier` that enforces `is_successor_of` requires the parent block to be present and is only run during full contextual verification — after the block is already in the orphan pool: [4](#0-3) 

Therefore, a block with epoch = 0 whose parent has epoch = `tip_epoch` passes `non_contextual_verify` and is inserted into the orphan pool without any epoch-continuity check.

### Impact Explanation

An attacker repeatedly sends pairs of crafted blocks via P2P block relay:

- **Shield block** (level-1): `parent = unknown_leader_hash`, `epoch = tip_epoch` — passes `need_clean` check, returns `false`.
- **Expired block** (level-2): `parent = shield_block.hash`, `epoch = 0` — never evicted because the subtree is skipped.

Each 60-second tick of `clean_expired_orphan_timer` calls `clean_expired_orphans` → `clean_expired_blocks`, but the expired level-2 blocks are never returned: [5](#0-4) [6](#0-5) 

The orphan pool grows without bound. Both `blocks` and `parents` HashMaps accumulate entries indefinitely, leading to memory exhaustion and eventual OOM of the node process.

### Likelihood Explanation

- Any unauthenticated P2P peer can send `SendBlock` messages.
- Crafted blocks need only satisfy `BlockVerifier` (cellbase structure, merkle root, size limits) — no valid PoW is required at the non-contextual stage.
- The attack is cheap to automate: send pairs of blocks in a loop, each with a fresh unknown parent hash.
- The cleanup timer fires every 60 seconds but is permanently ineffective for these subtrees.

### Recommendation

`need_clean` must check **all** direct children of the leader (not just the first), and should also recursively check descendants, or alternatively use the **minimum** epoch across all blocks in the subtree. A simpler fix: iterate all entries in the child map and return `true` only if **every** direct child is expired, or alternatively return `true` if **any** descendant is expired. The safest approach is to scan all descendants during cleanup rather than relying on a single-sample heuristic.

### Proof of Concept

```
tip_epoch = T

1. Craft Block C:
     parent_hash = <random unknown hash H>
     epoch       = T   (recent, not expired)
     (valid cellbase, merkle root, etc.)

2. Craft Block D:
     parent_hash = hash(C)
     epoch       = 0   (expired: 0 + 6 < T for T >= 7)
     (valid cellbase, merkle root, etc.)

3. Send C then D to the node via P2P SendBlock.
   Both pass non_contextual_verify (BlockVerifier checks no epoch ordering).
   H becomes a leader; C and D are inserted into the orphan pool.

4. Wait for clean_expired_orphan_timer (60s).
   clean_expired_blocks(T) runs:
     - leader = H
     - need_clean(H, T): checks C → T + 6 < T → false → skip
   D (epoch=0) remains in pool.

5. Repeat with fresh H' values indefinitely.
   Pool grows without bound → OOM.

Assert: after step 4, pool.len() >= 2 and D is still present.
``` [2](#0-1) [1](#0-0)

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

**File:** verification/src/header_verifier.rs (L133-148)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        if !self.header.epoch().is_well_formed() {
            return Err(EpochError::Malformed {
                value: self.header.epoch(),
            }
            .into());
        }
        if !self.parent.is_genesis() && !self.header.epoch().is_successor_of(self.parent) {
            return Err(EpochError::NonContinuous {
                current: self.header.epoch(),
                parent: self.parent,
            }
            .into());
        }
        Ok(())
    }
```

**File:** chain/src/chain_service.rs (L61-63)
```rust
                recv(clean_expired_orphan_timer) -> _ => {
                    self.orphan_broker.clean_expired_orphans();
                },
```

**File:** chain/src/orphan_broker.rs (L134-155)
```rust
    pub(crate) fn clean_expired_orphans(&self) {
        debug!("clean expired orphans");
        let tip_epoch_number = self
            .shared
            .store()
            .get_tip_header()
            .expect("tip header")
            .epoch()
            .number();
        let expired_orphans = self
            .orphan_blocks_broker
            .clean_expired_blocks(tip_epoch_number);
        for expired_orphan in expired_orphans {
            self.delete_block(&expired_orphan);
            self.shared.remove_header_view(&expired_orphan.hash());
            self.shared.remove_block_status(&expired_orphan.hash());
            info!(
                "cleaned expired orphan: {}-{}",
                expired_orphan.number(),
                expired_orphan.hash()
            );
        }
```
