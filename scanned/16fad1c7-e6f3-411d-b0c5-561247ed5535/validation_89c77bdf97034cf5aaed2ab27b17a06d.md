Audit Report

## Title
Unbounded Orphan Block Pool Growth via Missing Hard Size Cap and Off-by-One Expiry Predicate — (`chain/src/utils/orphan_block_pool.rs`, `chain/src/chain_service.rs`)

## Summary

The orphan block pool has no insertion-time size limit; `with_capacity` is a `HashMap` pre-allocation hint, not a hard cap. The sole eviction path (`clean_expired_blocks`) contains a strict `<` comparison that leaves blocks at `epoch_number = tip_epoch - EXPIRED_EPOCH` permanently in the pool. Combined with the fact that every accepted block is written to the chain DB before orphan pool insertion, a sufficiently resourced attacker can grow both heap memory and disk usage without bound, crashing the node.

## Finding Description

**Off-by-one in `need_clean`** (`chain/src/utils/orphan_block_pool.rs`, line 118):

```rust
lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
```

With `EXPIRED_EPOCH = 6`, a block at epoch `tip_epoch - 6` evaluates as `tip_epoch - 6 + 6 < tip_epoch` → `tip_epoch < tip_epoch` → `false`. Blocks must be at least 7 epochs old (not 6) to be evicted. The attacker targets `epoch_number = tip_epoch - 6` (or `tip_epoch - 5`), both of which permanently survive every clean cycle.

**No hard size cap** (`chain/src/utils/orphan_block_pool.rs`, lines 28–34 and 36–54):

`InnerPool::with_capacity` calls `HashMap::with_capacity`, which is a memory pre-allocation hint. `InnerPool::insert` performs zero size checks before inserting into `blocks`, `parents`, and `leaders`. The pool is initialized with `ORPHAN_BLOCK_SIZE = BLOCK_DOWNLOAD_WINDOW as usize = 8192` as a hint only (`chain/src/init.rs`, line 22–43), not a limit.

**DB write precedes orphan pool insertion** (`chain/src/chain_service.rs`, lines 133–143):

`insert_block` commits the block to the chain DB before `process_lonely_block` routes it into the orphan pool. Every orphan block therefore consumes both heap memory and persistent disk space.

**60-second timer is the sole eviction path** (`chain/src/chain_service.rs`, lines 40–63):

`clean_expired_orphans` fires on a 60-second tick and is the only mechanism that removes blocks. Blocks in the safe zone are never deleted; `delete_unverified_block` is only called for blocks that pass the (broken) expiry predicate.

## Impact Explanation

Sustained flooding causes the `InnerPool` `blocks`, `parents`, and `leaders` maps to grow without bound in heap memory, while the chain DB accumulates orphan block rows on disk. This leads to OOM or disk-full conditions, crashing or stalling the node. This matches the **High (10001–15000 points)** impact class: *Vulnerabilities which could easily crash a CKB node*.

## Likelihood Explanation

The attacker must be a P2P peer capable of sending blocks that pass `non_contextual_verify` (`BlockVerifier` + `NonContextualBlockTxsVerifier`). These checks are non-contextual and do not validate the block's epoch number against the chain tip, but they do include PoW verification. This means the attacker requires real mining capability — the attack is not free. However, a motivated attacker with modest hash power (or operating on a low-difficulty network) can mine valid orphan blocks at the boundary epoch and continuously submit them. As the tip epoch advances (~4 hours per epoch), the attacker adjusts the epoch field of new blocks to stay in the safe zone. The structural absence of a size cap means there is no ceiling on damage regardless of rate.

## Recommendation

1. **Enforce a hard insertion-time cap in `InnerPool::insert`**: Reject or evict the oldest entry when `self.parents.len() >= MAX_ORPHAN_POOL_SIZE`. This is the standard approach used by Bitcoin Core and other implementations.
2. **Fix the off-by-one**: Change `lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch` to `<=` in `need_clean` so blocks exactly 6 epochs old are also evicted.
3. **Per-peer orphan accounting**: Track per-peer orphan contribution counts and apply per-peer limits to prevent a single peer from monopolizing the pool.

## Proof of Concept

1. Connect to a CKB node as a P2P peer.
2. Read the current tip epoch number `T` via RPC.
3. Mine `N` valid block headers (passing PoW) with `epoch_number = T - 6`, each referencing a random unknown parent hash to guarantee orphan status.
4. Send each block via the standard block-relay P2P message.
5. Wait through 10 clean cycles (10 × 60 seconds).
6. Query `orphan_blocks_len()` via the `sync_state` RPC.
7. Assert: pool size ≈ N (not 0), confirming blocks at the boundary epoch were never evicted.
8. Repeat with increasing N until node OOM-crashes or disk fills.