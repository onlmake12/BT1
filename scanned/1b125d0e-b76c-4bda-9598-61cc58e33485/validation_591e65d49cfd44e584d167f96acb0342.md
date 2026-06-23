### Title
Incomplete Orphan Block Pruning Due to Single-Element Expiry Check — (`chain/src/utils/orphan_block_pool.rs`)

---

### Summary

`InnerPool::need_clean` in `chain/src/utils/orphan_block_pool.rs` checks only the **first arbitrarily-ordered element** of a `HashMap` group to decide whether to evict the **entire group** of orphan blocks sharing a parent hash. This is the direct Rust analog of the Solidity swap-and-pop bug: just as the Solidity loop skips re-checking the newly placed element after removal, `need_clean` skips checking all but one element of the group. An unprivileged peer can exploit this by sending two competing orphan blocks with the same parent but different epoch numbers, permanently preventing cleanup of the expired block and causing unbounded growth of the orphan pool, `block_status_map`, and `header_view`.

---

### Finding Description

`InnerPool` stores orphan blocks grouped by parent hash:

```
blocks: HashMap<ParentHash, HashMap<packed::Byte32, LonelyBlockHash>>
```

Multiple competing orphan blocks (forks at the same height) can share the same parent hash and occupy the same inner `HashMap`. The expiry check is: [1](#0-0) 

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .and_then(|map| {
            map.iter().next().map(|(_, lonely_block)| {   // ← only ONE element checked
                lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
            })
        })
        .unwrap_or_default()
}
```

`map.iter().next()` returns an **arbitrary** entry from the inner `HashMap` (Rust's `HashMap` has non-deterministic iteration order). The epoch number of that single block is used to decide whether to remove **all** blocks in the group. If the arbitrarily selected block is not expired, the entire group is kept — including any expired blocks also present in the group.

`clean_expired_blocks` calls `need_clean` for every leader and removes the group only if `need_clean` returns `true`: [2](#0-1) 

The cleanup is triggered periodically (every 60 seconds) from `ChainService`: [3](#0-2) 

When expired orphans are not returned by `clean_expired_blocks`, `clean_expired_orphans` in `OrphanBroker` never calls `delete_block`, `remove_header_view`, or `remove_block_status` for them: [4](#0-3) 

---

### Impact Explanation

Expired orphan blocks that are not pruned permanently occupy memory in three structures: `InnerPool::blocks`, `InnerPool::parents`, and the shared `block_status_map` and `header_view` caches. Because the orphan pool has no enforced maximum size (the `with_capacity` argument sets only the initial allocation), an attacker can grow these structures without bound. Each 60-second cleanup cycle that fails to evict a group leaves the stale entries permanently, since the expiry condition (`epoch_number + EXPIRED_EPOCH < tip_epoch`) only becomes more true over time — but the non-expired competing block in the group will always prevent the check from firing.

---

### Likelihood Explanation

Any peer connected to a CKB node can relay blocks. Sending two competing orphan blocks with the same parent hash requires only that the attacker produce two valid (non-contextually) block headers pointing to the same parent. The epoch field in a CKB block header is set by the block creator; non-contextual verification validates the field's encoding format but not its value against the actual chain state. An attacker can therefore set one block's epoch to a recent value (not expired) and another's to an old value (expired), ensuring the group is never cleaned regardless of which element `map.iter().next()` happens to return. The attack is cheap, repeatable, and requires no privileged access.

---

### Recommendation

Replace the single-element check with a check over **all** elements in the group, or use the **minimum** epoch number among all blocks under the parent (the most conservative choice — only clean if every block is expired):

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .map(|map| {
            map.values().all(|lonely_block| {
                lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
            })
        })
        .unwrap_or_default()
}
```

Alternatively, track the minimum epoch number per group at insertion time to avoid iterating the inner map on every cleanup cycle.

---

### Proof of Concept

1. Attacker connects to a CKB node as a peer.
2. Attacker crafts **Block A**: parent = `P` (unknown to the node), epoch = `1` (old, will be expired when `tip_epoch > 1 + EXPIRED_EPOCH = 7`).
3. Attacker crafts **Block B**: parent = `P`, epoch = `9999` (far future, never expired under normal operation).
4. Both blocks pass non-contextual verification (epoch field is validly encoded) and are inserted into the orphan pool under the same parent key `P`. The inner map for `P` now contains `{hash_A: BlockA, hash_B: BlockB}`.
5. The chain advances past epoch 7. `clean_expired_blocks` fires.
6. `need_clean(P, tip_epoch=8)` calls `map.iter().next()`. Due to HashMap non-determinism, it may return Block B (epoch 9999). `9999 + 6 < 8` is false → group is not cleaned.
7. Block A (epoch 1, genuinely expired) remains in the orphan pool, `block_status_map`, and `header_view` indefinitely.
8. Repeating steps 2–4 with fresh parent hashes `P1, P2, …` causes unbounded memory growth with no cleanup path.

### Citations

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

**File:** chain/src/utils/orphan_block_pool.rs (L112-122)
```rust
    /// get 1st block belongs to that parent and check if it's expired block
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

**File:** chain/src/chain_service.rs (L61-63)
```rust
                recv(clean_expired_orphan_timer) -> _ => {
                    self.orphan_broker.clean_expired_orphans();
                },
```

**File:** chain/src/orphan_broker.rs (L134-156)
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
    }
```
