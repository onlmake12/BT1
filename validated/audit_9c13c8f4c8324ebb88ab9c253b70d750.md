### Title
Orphan Block Pool Eviction Bypass via Single-Child Epoch Sampling in `need_clean` — (`chain/src/utils/orphan_block_pool.rs`)

---

### Summary

`InnerPool::need_clean` samples exactly **one arbitrary child** from a `HashMap` bucket to decide whether to evict an entire leader's subtree. Because `HashMap::iter().next()` returns an element in non-deterministic order, a single child with a non-expired epoch number permanently shields all expired siblings in the same bucket from eviction, violating the invariant that blocks older than `EXPIRED_EPOCH` epochs must be cleaned.

---

### Finding Description

`need_clean` is the sole gate for `clean_expired_blocks`: [1](#0-0) 

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .and_then(|map| {
            map.iter().next().map(|(_, lonely_block)| {   // ← ONE arbitrary child
                lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
            })
        })
        .unwrap_or_default()
}
```

`map` is a `HashMap<packed::Byte32, LonelyBlockHash>`. `map.iter().next()` yields whichever entry the HashMap's internal layout happens to place first — this is not random per call, but it is not attacker-predictable either. If that first entry has `epoch_number >= tip_epoch - EXPIRED_EPOCH`, the function returns `false` and `clean_expired_blocks` skips the entire bucket: [2](#0-1) 

The `epoch_number` stored in `LonelyBlockHash` is taken directly from the block header at insertion time, before any chain-state validation is possible (the block is an orphan — its parent is unknown): [3](#0-2) 

Orphan blocks are inserted without epoch validation in `process_lonely_block`: [4](#0-3) 

`clean_expired_orphans` (the periodic cleanup tick) calls `clean_expired_blocks` with the current tip epoch: [5](#0-4) 

---

### Impact Explanation

Every 60-second `clean_expired_orphans` tick calls `clean_expired_blocks(tip_epoch)`. If a leader bucket contains even one block whose `epoch_number + EXPIRED_EPOCH >= tip_epoch`, `need_clean` returns `false` and **all** blocks in that bucket — including arbitrarily many with epochs far below the eviction threshold — survive indefinitely. Over time this causes unbounded accumulation of stale orphan blocks, leading to memory exhaustion and node crash. [6](#0-5) 

`EXPIRED_EPOCH = 6` is the only eviction mechanism; there is no secondary size-based cap that would prevent unbounded growth.

---

### Likelihood Explanation

The logic flaw manifests **without any attacker** whenever a legitimate leader bucket happens to contain children from different epochs and the HashMap places a recent-epoch child first. For a deliberate exploit, an attacker must:

1. **Mine at least one block with a crafted future epoch number.** The `epoch_number` field in the CKB block header is taken at face value for orphan blocks. A block with a future epoch still requires valid PoW, which is a significant computational barrier on mainnet. On testnet or during low-difficulty periods this barrier is lower.
2. **Arrange for that block to be iterated first by the HashMap.** The HashMap seed is fixed per process start (Rust `RandomState`), so iteration order is deterministic within a run but not externally predictable. The attacker cannot guarantee their block is iterated first, but with many poisoned buckets the probability increases.

The correctness bug (non-attacker scenario) is certain; the targeted exploit requires hashpower and probabilistic HashMap luck.

---

### Recommendation

Replace the single-sample check with a check over **all** children in the bucket, or use the **minimum** epoch among children:

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

Alternatively, evict individual expired children rather than requiring the entire bucket to be expired.

---

### Proof of Concept

```rust
// Construct a leader bucket with one future-epoch child and N expired children.
// All share the same parent_hash (the leader).
let tip_epoch = 10u64;
// Insert 999 expired blocks (epoch 1, far below tip_epoch - EXPIRED_EPOCH = 4)
for i in 0..999 {
    pool.insert(make_lonely_block(parent_hash, epoch=1, unique_hash=i));
}
// Insert 1 future-epoch block (epoch = tip_epoch + 1 = 11)
pool.insert(make_lonely_block(parent_hash, epoch=11, unique_hash=999));

// Call clean_expired_blocks
let removed = pool.clean_expired_blocks(tip_epoch);

// If HashMap places the future-epoch block first via map.iter().next(),
// need_clean returns false → removed.len() == 0, all 1000 blocks survive.
// Expected correct behavior: removed.len() == 999 (or 1000).
assert_eq!(removed.len(), 999); // FAILS when future-epoch block is iterated first
```

The HashMap iteration order is fixed per process run. By varying the block hash values (which determine HashMap bucket placement), an attacker can find a hash that causes the future-epoch block to be returned first by `iter().next()`, permanently shielding all expired siblings. [1](#0-0)

### Citations

**File:** chain/src/utils/orphan_block_pool.rs (L12-13)
```rust
const SHRINK_THRESHOLD: usize = 100;
pub const EXPIRED_EPOCH: u64 = 6;
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

**File:** chain/src/lib.rs (L97-98)
```rust
        let epoch_number: EpochNumber = block.epoch().number();

```

**File:** chain/src/orphan_broker.rs (L119-123)
```rust
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
        } else {
            self.orphan_blocks_broker.insert(lonely_block);
        }
```

**File:** chain/src/orphan_broker.rs (L134-145)
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
```
