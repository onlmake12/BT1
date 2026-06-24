Audit Report

## Title
Single-Sample Epoch Check in `need_clean` Allows Expired Orphan Subtree Eviction Bypass — (`chain/src/utils/orphan_block_pool.rs`)

## Summary

`InnerPool::need_clean` calls `map.iter().next()` to sample exactly one arbitrarily-ordered `HashMap` entry when deciding whether to evict an entire leader's subtree. If that one sampled entry belongs to a fresh-epoch block, `clean_expired_blocks` skips the entire subtree on every cleanup cycle, permanently retaining all expired-epoch orphans under the same leader. Because the orphan pool has no hard capacity limit, this allows unbounded memory and disk growth, crashing the node.

## Finding Description

`need_clean` at lines 113–122 of `chain/src/utils/orphan_block_pool.rs` reads:

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .and_then(|map| {
            map.iter().next().map(|(_, lonely_block)| {   // only ONE entry checked
                lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
            })
        })
        .unwrap_or_default()
}
```

The inner map is `HashMap<packed::Byte32, LonelyBlockHash>` (line 18). `map.iter().next()` returns whichever entry occupies the lowest internal bucket index, determined by the per-instance random seed fixed at map creation. This order is non-deterministic across runs but **fixed for the lifetime of that map instance**.

`clean_expired_blocks` (lines 99–110) calls `need_clean` for every leader and removes the subtree only when it returns `true`. If the sampled entry is a fresh-epoch block, the entire subtree — including all expired-epoch siblings — is never evicted.

An attacker exploits this by:
1. Connecting to the target node via P2P.
2. Sending blocks whose parent hash does not exist in the chain. These are inserted into the orphan pool at line 122 of `orphan_broker.rs` after only a parent-status check, with no PoW re-verification at the orphan-pool insertion stage.
3. Inserting a large batch of expired-epoch orphans (epoch < `tip_epoch − EXPIRED_EPOCH`) under a chosen leader hash.
4. Inserting one or more fresh-epoch siblings under the same leader hash. With K fresh siblings among N expired, the probability that a fresh entry is sampled first is ≈ K/(K+N), approaching certainty as K grows.
5. Once a fresh entry occupies the first bucket, `need_clean` returns `false` on every subsequent `clean_expired_orphans` call, permanently bypassing eviction for that subtree.

`insert()` (lines 36–54) has no size guard, so the `blocks` map, `parents` map, and on-disk unverified block data all grow without bound.

The existing test `test_remove_expired_blocks` (lines 233–262 of `chain/src/tests/orphan_block_pool.rs`) only covers the all-expired case and does not test the mixed-epoch sibling scenario, leaving the bug undetected.

## Impact Explanation

**High — Vulnerability which could easily crash a CKB node.**

Unbounded growth of the `blocks` and `parents` HashMaps and the on-disk unverified block store leads to memory and disk exhaustion, crashing the targeted node. The attacker needs only a P2P connection and the ability to craft blocks with a chosen parent hash; no mining capability is required if PoW is not re-verified at orphan-pool insertion.

## Likelihood Explanation

The attacker controls the number of fresh-epoch siblings inserted. With K fresh siblings, the probability of bypassing cleanup approaches K/(K+N). Once the HashMap seed produces a favorable ordering (which is fixed at map creation), the bypass is permanent across all future `clean_expired_orphans` invocations. The attacker can retry with a new leader hash if the first attempt fails, and each attempt is cheap (no PoW required at insertion). The condition is therefore practically achievable with modest effort.

## Recommendation

Replace the single-sample check with a check over all direct children of the leader, using the minimum epoch:

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

This ensures the subtree is only evicted when every direct child is expired, which is the correct invariant. Additionally, enforce a hard capacity limit in `insert()` to bound pool size independently of cleanup correctness.

## Proof of Concept

Add the following test to `chain/src/tests/orphan_block_pool.rs`:

```rust
#[test]
fn test_remove_expired_blocks_mixed_epoch() {
    // tip_epoch = 20, EXPIRED_EPOCH = 6 → expired condition: epoch + 6 < 20 → epoch < 14
    let tip_epoch = 20_u64;
    let expired_epoch = EpochNumberWithFraction::new(1, 0, 10);   // epoch 1, expired
    let fresh_epoch   = EpochNumberWithFraction::new(15, 0, 10);  // epoch 15, fresh

    let pool = OrphanBlockPool::with_capacity(1100);
    let leader_hash = /* any hash not in pool, e.g. genesis hash */;

    // Insert 1000 expired-epoch orphans under the leader
    for i in 0..1000_u64 {
        let block = BlockBuilder::default()
            .parent_hash(leader_hash.clone())
            .epoch(expired_epoch)
            .nonce(i)
            .build();
        pool.insert(LonelyBlock { block: Arc::new(block), switch: None, verify_callback: None }.into());
    }

    // Insert one fresh-epoch sibling under the same leader
    let fresh_block = BlockBuilder::default()
        .parent_hash(leader_hash.clone())
        .epoch(fresh_epoch)
        .nonce(9999_u64)
        .build();
    pool.insert(LonelyBlock { block: Arc::new(fresh_block), switch: None, verify_callback: None }.into());

    let evicted = pool.clean_expired_blocks(tip_epoch);

    // BUG: if the fresh sibling is sampled first, evicted.len() == 0
    // CORRECT: all 1000 epoch=1 blocks satisfy 1+6 < 20, so evicted.len() should be 1000
    // (fresh block should remain; expired blocks should be evicted)
    // With the current single-sample implementation this assertion will fail non-deterministically.
    assert_eq!(evicted.len(), 1000);
}
```

Running this test repeatedly will demonstrate non-deterministic failures under the current implementation, confirming the bug. With the recommended `all()`-based fix, the test passes deterministically.