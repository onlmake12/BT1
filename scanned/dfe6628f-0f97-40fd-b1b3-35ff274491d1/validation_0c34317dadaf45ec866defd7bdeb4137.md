Audit Report

## Title
Orphan Block Pool Memory Exhaustion via Single-Child Sampling in `need_clean` — (`chain/src/utils/orphan_block_pool.rs`)

## Summary

`need_clean` uses `map.iter().next()` to sample a single, non-deterministically ordered child from a sibling group and decides whether to evict the entire group based solely on that one child's `epoch_number`. An attacker can insert a crafted orphan block with a far-future `epoch_number` under any unknown parent hash; if that block wins the HashMap iteration lottery, the entire sibling group — including genuinely expired blocks — is never cleaned. Because the orphan block pool has no hard size cap and non-contextual block verification does not check PoW or epoch correctness, the attacker can flood the pool at CPU speed with zero mining work, causing unbounded memory growth and eventual node crash.

## Finding Description

**Root cause — `need_clean` samples one arbitrary child:**

`InnerPool::need_clean` calls `map.iter().next()` on the inner `HashMap<packed::Byte32, LonelyBlockHash>` to pick a representative child for the leader group. [1](#0-0) 

Rust's `HashMap` iteration order is non-deterministic (random seed per process). If the first-returned child has `epoch_number + EXPIRED_EPOCH >= tip_epoch`, the function returns `false` and `clean_expired_blocks` skips the entire group, including siblings whose epoch is genuinely expired. [2](#0-1) 

**`epoch_number` is taken verbatim from the attacker-supplied block header:**

When a `LonelyBlock` is converted to `LonelyBlockHash`, the epoch number is read directly from the block header with no validation: [3](#0-2) 

**Non-contextual block verification does not check PoW or epoch correctness:**

`ChainService::non_contextual_verify` calls only `BlockVerifier` and `NonContextualBlockTxsVerifier`. `BlockVerifier` covers cellbase, block bytes, extension, proposals limit, duplicates, and merkle root — it does not include `HeaderVerifier` or `PowVerifier`. [4](#0-3) [5](#0-4) 

`HeaderVerifier` — which runs `PowVerifier` and the context-dependent `EpochVerifier` — requires the parent header and is only invoked in the sync header-processing path, not for orphan blocks: [6](#0-5) 

The contextual `EpochVerifier` in `contextual_block_verifier.rs` that validates both `epoch_number` and `compact_target` against the actual chain state is never reached for orphan blocks: [7](#0-6) 

**No hard cap on orphan block pool size:**

`OrphanBlockPool::insert` has no size guard. The `with_capacity` argument is only a HashMap pre-allocation hint. Compare with the tx orphan pool, which enforces `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` and actively evicts on overflow: [8](#0-7) [9](#0-8) [10](#0-9) 

**Cleanup timer fires every 60 seconds:** [11](#0-10) 

## Impact Explanation

An attacker can grow the orphan block pool without bound, consuming all available memory and crashing the targeted CKB node. This matches the allowed bounty impact: **"Vulnerabilities which could easily crash a CKB node" — High (10001–15000 points)**.

## Likelihood Explanation

- **Zero mining work required.** Non-contextual block verification does not run `PowVerifier` for orphan blocks, so any nonce is accepted. The attacker does not need to satisfy any PoW condition.
- **No privileged access.** The attack path is the standard P2P block-relay path: `Synchronizer::received` → `asynchronous_process_block` → `process_lonely_block` → `orphan_blocks_broker.insert`.
- **Deterministic shielding.** Once a far-future-epoch block wins the HashMap iteration order for a given leader, it shields its siblings permanently for the lifetime of the process. The attacker can use a single crafted block per leader (no sibling trick needed) with `epoch_number = u64::MAX - EXPIRED_EPOCH - 1` to guarantee `need_clean` always returns `false`.
- **Repeatable at CPU speed.** The attacker generates fresh unknown parent hashes in a tight loop, inserting one block per leader, and the pool grows by one entry per iteration with no cleanup ever occurring.

## Recommendation

1. **Fix `need_clean` to check all children:** return `true` if *any* child satisfies `epoch_number + EXPIRED_EPOCH < tip_epoch`, or better, evict individual expired children rather than the whole group atomically.
2. **Enforce a hard cap on orphan block pool size** analogous to `DEFAULT_MAX_ORPHAN_TRANSACTIONS` in the tx orphan pool, with LRU or oldest-epoch eviction when the cap is reached.
3. **Add a minimum `compact_target` check in non-contextual verification** (or at the P2P ingress layer) to reject blocks whose declared difficulty is implausibly low relative to the genesis target, preventing zero-work orphan spam even if the cleanup logic is fixed.

## Proof of Concept

```rust
// Mirrors the existing test harness in chain/src/tests/orphan_block_pool.rs
let pool = OrphanBlockPool::with_capacity(10);
let tip_epoch: EpochNumber = 20;

// Craft two orphans sharing an unknown parent.
// B1: far-future epoch — shields the group. No PoW needed (BlockVerifier doesn't check it).
let b1 = make_lonely_block_hash(
    /*parent*/ random_byte32(),
    /*epoch_number*/ u64::MAX - EXPIRED_EPOCH - 1,
);
// B2: expired epoch — should be cleaned but won't be if B1 is iterated first.
let b2 = make_lonely_block_hash(
    /*parent*/ b1.parent_hash(),
    /*epoch_number*/ tip_epoch - 10,
);

pool.insert(b1);
pool.insert(b2);

let cleaned = pool.clean_expired_blocks(tip_epoch);
// If HashMap returns B1 first: cleaned.len() == 0  ← invariant violated
// If HashMap returns B2 first: cleaned.len() == 2
// The guarantee "all blocks with epoch+EXPIRED_EPOCH < tip_epoch are cleaned"
// is NOT upheld. Repeating with fresh parent hashes fills the pool unboundedly.
assert_eq!(cleaned.len(), 2); // NOT guaranteed to hold
```

Flood loop (pseudocode):
```rust
loop {
    let parent = random_byte32(); // unknown to victim
    pool.insert(make_lonely_block_hash(parent, u64::MAX - EXPIRED_EPOCH - 1));
    // pool.len() grows by 1 every iteration; clean_expired_blocks never removes it
}
```

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

**File:** chain/src/utils/orphan_block_pool.rs (L140-143)
```rust
    /// Insert orphaned block, for which we have already requested its parent block
    pub fn insert(&self, lonely_block: LonelyBlockHash) {
        self.inner.write().insert(lonely_block);
    }
```

**File:** chain/src/lib.rs (L97-106)
```rust
        let epoch_number: EpochNumber = block.epoch().number();

        LonelyBlockHash {
            block_number_and_hash: BlockNumberAndHash {
                number: block_number,
                hash: block_hash,
            },
            parent_hash,
            epoch_number,
            switch,
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

**File:** chain/src/chain_service.rs (L40-41)
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

**File:** verification/src/header_verifier.rs (L30-51)
```rust
impl<'a, DL: HeaderFieldsProvider> Verifier for HeaderVerifier<'a, DL> {
    type Target = HeaderView;
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
        let parent_fields = self
            .data_loader
            .get_header_fields(&header.parent_hash())
            .ok_or_else(|| UnknownParentError {
                parent_hash: header.parent_hash(),
            })?;
        NumberVerifier::new(parent_fields.number, header).verify()?;
        EpochVerifier::new(parent_fields.epoch, header).verify()?;
        TimestampVerifier::new(
            self.data_loader,
            header,
            self.consensus.median_time_block_count(),
        )
        .verify()?;
        Ok(())
    }
}
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L488-509)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let header = self.block.header();
        let actual_epoch_with_fraction = header.epoch();
        let block_number = header.number();
        let epoch_with_fraction = self.epoch.number_with_fraction(block_number);
        if actual_epoch_with_fraction != epoch_with_fraction {
            return Err(EpochError::NumberMismatch {
                expected: epoch_with_fraction.full_value(),
                actual: actual_epoch_with_fraction.full_value(),
            }
            .into());
        }
        let actual_compact_target = header.compact_target();
        if self.epoch.compact_target() != actual_compact_target {
            return Err(EpochError::TargetMismatch {
                expected: self.epoch.compact_target(),
                actual: actual_compact_target,
            }
            .into());
        }
        Ok(())
    }
```

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L119-125)
```rust
        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }
```
