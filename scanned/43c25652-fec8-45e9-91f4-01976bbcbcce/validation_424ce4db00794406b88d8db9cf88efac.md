### Title
Orphan Pool Cleanup Bypass via Single Fresh-Epoch Sibling — (`chain/src/utils/orphan_block_pool.rs`)

### Summary

`InnerPool::need_clean` samples only one arbitrarily-ordered `HashMap` entry to decide whether an entire leader's subtree should be evicted. Because Rust's `HashMap` iteration order is non-deterministic across insertions, a remote peer that inserts a single fresh-epoch sibling under the same leader as a large set of expired-epoch orphans has a non-zero probability of permanently anchoring that fresh entry as the first bucket entry, causing `clean_expired_blocks` to skip the entire subtree on every subsequent cleanup cycle.

---

### Finding Description

`need_clean` is implemented as:

```rust
// chain/src/utils/orphan_block_pool.rs  lines 113-122
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .and_then(|map| {
            map.iter().next().map(|(_, lonely_block)| {   // ← only ONE entry checked
                lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
            })
        })
        .unwrap_or_default()
}
``` [1](#0-0) 

The inner map is `HashMap<packed::Byte32, LonelyBlockHash>` — a hash map keyed by block hash. `map.iter().next()` returns whichever entry happens to occupy the lowest internal bucket index, which is a function of the per-`HashMap`-instance random seed (set once at creation) and the key hashes. This order is **fixed for the lifetime of that map instance** but is not controlled by the caller.

`clean_expired_blocks` calls `need_clean` for every leader and removes the subtree only when it returns `true`:

```rust
// lines 99-110
pub fn clean_expired_blocks(&mut self, tip_epoch: EpochNumber) -> Vec<LonelyBlockHash> {
    let mut result = vec![];
    for hash in self.leaders.clone().iter() {
        if self.need_clean(hash, tip_epoch) {
            let descendants = self.remove_blocks_by_parent(hash);
            result.extend(descendants);
        }
    }
    result
}
``` [2](#0-1) 

If the fresh-epoch sibling's hash lands in the first bucket of the inner `HashMap`, `need_clean` returns `false` on every call, and the entire subtree — including all expired-epoch orphans — is never evicted.

---

### Impact Explanation

The orphan pool has no hard capacity enforcement:

```rust
// line 36-54 — insert() has no size guard
fn insert(&mut self, lonely_block: LonelyBlockHash) { ... }
``` [3](#0-2) 

An attacker who successfully anchors the fresh sibling as the first bucket entry can accumulate an unbounded number of expired orphan blocks in memory. The `parents` map, `blocks` map, and the stored unverified block data on disk all grow without bound, constituting a memory/disk exhaustion DoS.

---

### Likelihood Explanation

**Probabilistic but amplifiable.** With exactly one fresh sibling among N expired siblings, the probability that the fresh entry occupies the first bucket is approximately 1/(N+1). However:

1. The HashMap seed is fixed at creation; if the condition is met once, it holds permanently for that leader's subtree.
2. The attacker can insert **multiple** fresh-epoch siblings to increase the probability toward certainty (K fresh siblings among N expired → probability ≈ K/(K+N)).
3. Blocks are admitted to the orphan pool via P2P sync/relay (`orphan_broker.rs` line 122) after only a parent-status check — no PoW re-verification is required at the orphan-pool insertion stage. [4](#0-3) 

The `clean_expired_orphans` path in `OrphanBroker` calls `clean_expired_blocks` with the current tip epoch number, so the bypass persists across all periodic cleanup invocations. [5](#0-4) 

---

### Recommendation

Replace the single-sample check with a check over **all** direct children of the leader (or use the minimum epoch among them):

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

This ensures the subtree is only evicted when **every** direct child is expired, which is the correct invariant.

---

### Proof of Concept

```rust
// Pseudocode — directly testable against InnerPool
let mut pool = InnerPool::with_capacity(1100);
let leader_hash = /* some hash not in pool */;

// Insert 1000 expired-epoch orphans under the leader
for _ in 0..1000 {
    pool.insert(make_block(parent=leader_hash, epoch=1));
}

// Insert one fresh-epoch sibling under the same leader
pool.insert(make_block(parent=leader_hash, epoch=tip_epoch));  // epoch >= tip_epoch - EXPIRED_EPOCH

// tip_epoch = 20, EXPIRED_EPOCH = 6, so expired condition: epoch + 6 < 20 → epoch < 14
let evicted = pool.clean_expired_blocks(20);

// BUG: if the fresh sibling is the first HashMap entry, evicted.len() == 0
// INVARIANT: all 1000 epoch=1 blocks satisfy 1 + 6 < 20, so evicted.len() SHOULD be 1000
assert_eq!(evicted.len(), 0);   // demonstrates the bug when fresh entry wins the bucket race
```

The test `test_remove_expired_blocks` in `chain/src/tests/orphan_block_pool.rs` (lines 233–262) only tests the all-expired case and does not cover the mixed-epoch sibling scenario, so the bug is untested. [6](#0-5)

### Citations

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

**File:** chain/src/orphan_broker.rs (L119-123)
```rust
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
        } else {
            self.orphan_blocks_broker.insert(lonely_block);
        }
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

**File:** chain/src/tests/orphan_block_pool.rs (L233-262)
```rust
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
```
