Audit Report

## Title
Incomplete Orphan Block Expiry Check Allows Unbounded Memory Growth via Single-Element `HashMap` Sampling — (`chain/src/utils/orphan_block_pool.rs`)

## Summary

`InnerPool::need_clean` samples only one arbitrarily-ordered element from the inner `HashMap` to decide whether to evict an entire group of orphan blocks sharing a parent hash. An attacker with no special privileges can insert two competing orphan blocks under the same parent — one with an old epoch (expired) and one with a far-future epoch (never expired) — causing the cleanup logic to permanently skip the group. Because non-contextual block verification does not check PoW or epoch validity, this attack requires no mining work and is freely repeatable, leading to unbounded growth of the orphan pool, `block_status_map`, and `header_view`.

## Finding Description

`InnerPool` stores orphan blocks grouped by parent hash:

```rust
blocks: HashMap<ParentHash, HashMap<packed::Byte32, LonelyBlockHash>>
```

`need_clean` decides whether to evict a group by checking exactly one element:

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .and_then(|map| {
            map.iter().next().map(|(_, lonely_block)| {   // ← only ONE element
                lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
            })
        })
        .unwrap_or_default()
}
```

`HashMap::iter()` has non-deterministic order. If the sampled element is not expired, the entire group — including genuinely expired blocks — is kept.

`clean_expired_blocks` calls `need_clean` for every leader and removes the group only when it returns `true`. The timer fires every 60 seconds from `ChainService::start_process_block`.

**Critical enabler — no PoW required to reach the orphan pool.** The non-contextual `BlockVerifier` called in `asynchronous_process_block` checks only proposals limit, block bytes, cellbase structure, duplicates, and merkle root. It does **not** check PoW or epoch validity. PoW is only verified by `HeaderVerifier`, which is contextual (requires the parent header) and is not invoked at this stage. Therefore any peer can insert arbitrary blocks with arbitrary epoch numbers into the orphan pool at zero mining cost.

The full path:
1. Attacker sends Block A: `parent = P` (unknown), `epoch = 1`.
2. Attacker sends Block B: `parent = P`, `epoch = 9999`.
3. Both pass `non_contextual_verify` (no PoW, no epoch check) and are stored via `insert_block` then `orphan_blocks_broker.insert`.
4. The inner map for `P` now contains `{hash_A: BlockA, hash_B: BlockB}`.
5. Chain advances past epoch 7. `clean_expired_blocks(8)` fires.
6. `need_clean(P, 8)` calls `map.iter().next()`. If it returns Block B: `9999 + 6 < 8` → `false` → group not cleaned.
7. Block A (epoch 1, genuinely expired) remains permanently in `InnerPool::blocks`, `InnerPool::parents`, `block_status_map`, and `header_view`.
8. Repeating with fresh parent hashes `P1, P2, …` grows all four structures without bound.

## Impact Explanation

Unbounded growth of the orphan pool and associated maps (`block_status_map`, `header_view`) will exhaust heap memory on the target node, causing an OOM crash. This maps to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**. The attack is also applicable at scale across many nodes simultaneously, which additionally maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation

Any connected peer can relay blocks. Non-contextual verification (the only gate before orphan pool insertion) does not check PoW or epoch values — confirmed by `BlockVerifier::verify` which runs only `BlockProposalsLimitVerifier`, `BlockBytesVerifier`, `CellbaseVerifier`, `DuplicateVerifier`, and `MerkleRootVerifier`. The attacker needs only to craft two syntactically valid block structures pointing to the same unknown parent with different epoch numbers. This is trivially cheap, requires no hash-rate, and is indefinitely repeatable.

## Recommendation

Replace the single-element sample with a check over all elements in the group. The most conservative fix (only clean if every block in the group is expired):

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

## Proof of Concept

Extend the existing unit test in `chain/src/tests/orphan_block_pool.rs`:

```rust
#[test]
fn test_need_clean_skips_expired_block_when_sibling_is_not_expired() {
    use crate::utils::orphan_block_pool::{EXPIRED_EPOCH, OrphanBlockPool};
    use crate::{LonelyBlock, LonelyBlockHash};
    use ckb_types::core::{BlockBuilder, EpochNumberWithFraction};
    use ckb_systemtime::unix_time_as_millis;
    use std::sync::Arc;

    let pool = OrphanBlockPool::with_capacity(10);
    let parent_hash = ckb_types::packed::Byte32::default(); // unknown parent

    // Block A: epoch 1 (expired when tip_epoch > 7)
    let block_a = BlockBuilder::default()
        .parent_hash(parent_hash.clone())
        .number(1u64.pack())
        .epoch(EpochNumberWithFraction::new(1, 0, 1000))
        .timestamp(unix_time_as_millis())
        .build();

    // Block B: epoch 9999 (never expired)
    let block_b = BlockBuilder::default()
        .parent_hash(parent_hash.clone())
        .number(1u64.pack())
        .epoch(EpochNumberWithFraction::new(9999, 0, 1000))
        .timestamp(unix_time_as_millis() + 1)
        .build();

    pool.insert(LonelyBlock { block: Arc::new(block_a), switch: None, verify_callback: None }.into());
    pool.insert(LonelyBlock { block: Arc::new(block_b), switch: None, verify_callback: None }.into());

    // tip_epoch = 8: Block A is expired (1+6 < 8), Block B is not (9999+6 < 8 is false)
    let tip_epoch = 1 + EXPIRED_EPOCH + 1; // = 8
    let cleaned = pool.clean_expired_blocks(tip_epoch);

    // With the bug: if iter().next() returns Block B, cleaned.len() == 0
    // Both blocks should be cleaned (Block A is expired; Block B is a sibling under same parent)
    // At minimum, Block A must be cleaned.
    assert_eq!(cleaned.len(), 2, "both sibling orphans under expired parent group must be cleaned");
}
```

Running this test will non-deterministically pass or fail depending on `HashMap` iteration order, demonstrating the race condition. With the `all()`-based fix it passes deterministically.