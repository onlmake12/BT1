Audit Report

## Title
Orphan Block Pool Memory Exhaustion via Single-Child Sampling in `need_clean` on Non-Deterministic HashMap — (`chain/src/utils/orphan_block_pool.rs`)

## Summary

`need_clean` samples only the first entry of a `HashMap<packed::Byte32, LonelyBlockHash>` via `map.iter().next()` to decide whether an entire sibling group should be evicted. Because Rust's `HashMap` iteration order is non-deterministic, an attacker can insert a crafted orphan block with a far-future `epoch_number` alongside a legitimately-expired sibling under the same unknown parent. If the far-future block is iterated first, the entire group is permanently shielded from cleanup. Since the orphan block pool has no hard size cap and PoW is not verified during non-contextual processing, the attacker can flood the pool at CPU speed, exhausting node memory.

## Finding Description

**Root cause — `need_clean` samples one child:**

`InnerPool::need_clean` at `chain/src/utils/orphan_block_pool.rs:113-122` calls `map.iter().next()` on a `HashMap`, returning an arbitrary child of the leader group. If that child's `epoch_number + EXPIRED_EPOCH >= tip_epoch`, the function returns `false` and `clean_expired_blocks` skips the entire group — including siblings whose epoch is genuinely expired.

**`epoch_number` is taken verbatim from the attacker-supplied block header:**

At `chain/src/lib.rs:97`, `let epoch_number: EpochNumber = block.epoch().number();` is extracted directly from the block header with no validation before being stored in `LonelyBlockHash`.

**Epoch and PoW correctness are only checked in contextual verification, which never runs for orphan blocks:**

`ChainService::non_contextual_verify` (`chain/src/chain_service.rs:72-89`) calls only `BlockVerifier` and `NonContextualBlockTxsVerifier`. `BlockVerifier` covers cellbase, block bytes, extension, proposals limit, duplicates, and merkle root — it does not include `PowVerifier` or `EpochVerifier`. `HeaderVerifier` (which calls `PowVerifier` and `EpochVerifier`) requires a `data_loader` to resolve the parent header and is never invoked for orphan blocks. The contextual `EpochVerifier` (`verification/contextual/src/contextual_block_verifier.rs:488-509`) that would reject a mismatched `epoch_number` or `compact_target` is never reached.

**Consequence:** An attacker can submit a block with `epoch_number = u64::MAX - 5` and any nonce (no mining required). It passes non-contextual verification and enters the orphan pool. `need_clean` will always return `false` for it, permanently preventing cleanup of its sibling group.

**The cleanup timer fires every 60 seconds** (`chain/src/chain_service.rs:40-41`), giving the attacker a 60-second window per batch to insert more entries before any cleanup attempt.

**The orphan block pool has no hard block count limit.** `OrphanBlockPool::insert` (`chain/src/utils/orphan_block_pool.rs:141-143`) performs no size check. The `with_capacity` argument is only a `HashMap` allocation hint. By contrast, the tx orphan pool (`tx-pool/src/component/orphan.rs:16,119`) enforces `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` and evicts entries when exceeded.

## Impact Explanation

An attacker can grow the orphan block pool without bound by repeatedly submitting crafted blocks with unknown parent hashes and far-future epoch numbers. Each block is stored to disk (`insert_block` at `chain/src/chain_service.rs:133-141`) and held in memory in the `InnerPool`. Sustained flooding exhausts both memory and disk, crashing the node.

**Impact: High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

- No mining power is required; PoW is not checked during non-contextual verification for orphan blocks.
- No privileged access, leaked keys, or social engineering is needed.
- The attack is reachable via the standard P2P block-relay path: `Synchronizer::received` → `asynchronous_process_block` → `process_lonely_block` → `orphan_blocks_broker.insert`.
- The HashMap ordering is fixed for the lifetime of the process (random seed at startup). Once the "shield" block wins the ordering lottery, it permanently shields its sibling group until the process restarts.
- The attacker can use fresh unknown parent hashes for each batch, creating independent shielded groups, so the attack does not depend on a single lucky ordering.

## Recommendation

1. **Fix `need_clean` to check all children:** Return `true` if **any** child satisfies `epoch_number + EXPIRED_EPOCH < tip_epoch`, or better, evict individual expired children rather than the whole group atomically.
2. **Enforce a hard cap on orphan block pool size** and evict by oldest epoch when the cap is reached, analogous to `DEFAULT_MAX_ORPHAN_TRANSACTIONS` in the tx-pool.
3. **Add a minimum `compact_target` check in non-contextual verification** (or at minimum a standalone `PowVerifier` call in `BlockVerifier`) to reject blocks whose declared difficulty is implausibly low or whose PoW is invalid, preventing zero-cost orphan spam.

## Proof of Concept

```rust
// Mirrors the existing test in chain/src/tests/orphan_block_pool.rs
let pool = OrphanBlockPool::with_capacity(10);
let tip_epoch = 20_u64;
let unknown_parent = random_byte32(); // not in chain or pool

// B1: far-future epoch — shields the group
let b1 = make_lonely_block_hash(unknown_parent, epoch = tip_epoch + 100);
// B2: expired epoch — should be cleaned
let b2 = make_lonely_block_hash(unknown_parent, epoch = tip_epoch - 10);

pool.insert(b1);
pool.insert(b2);

// need_clean checks only map.iter().next():
// - If B1 is returned: (tip_epoch+100)+6 < tip_epoch → false → neither block cleaned
// - If B2 is returned: (tip_epoch-10)+6 < tip_epoch → true → both cleaned
let cleaned = pool.clean_expired_blocks(tip_epoch);
// The following assertion is NOT guaranteed to hold:
assert_eq!(cleaned.len(), 2);

// Repeat with fresh unknown_parent hashes to grow pool without bound.
// Each iteration requires zero mining work.
```