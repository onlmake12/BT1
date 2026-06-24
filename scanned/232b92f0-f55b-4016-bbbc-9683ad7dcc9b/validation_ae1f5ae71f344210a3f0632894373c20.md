Audit Report

## Title
Single-Child Epoch Sampling in `need_clean` Causes Incorrect Orphan Subtree Purge — (`chain/src/utils/orphan_block_pool.rs`)

## Summary
`InnerPool::need_clean` decides whether to purge an entire orphan subtree by inspecting only the **first** child returned by a non-deterministic `HashMap` iterator. An unprivileged P2P peer can insert two orphan blocks sharing the same parent hash but carrying different epoch numbers — both passing non-contextual verification — causing either premature purge of non-expired orphan blocks (sync disruption) or indefinite retention of expired blocks (orphan pool bloat). The root cause is confirmed in the actual source code.

## Finding Description
`need_clean` at `chain/src/utils/orphan_block_pool.rs` lines 113–122 calls `map.iter().next()` on a `HashMap`, whose iteration order is non-deterministic:

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

The comment above the function even acknowledges this: `"get 1st block belongs to that parent and check if it's expired block"`. The implicit assumption is that all children of a leader share the same epoch, but this is not enforced.

The `blocks` map is `HashMap<ParentHash, HashMap<packed::Byte32, LonelyBlockHash>>`, so multiple children with the same parent hash but different block hashes (and different epoch numbers) can coexist. The `insert` function at lines 36–54 places each child under the same `parent_hash` key with no epoch homogeneity check.

The non-contextual verifier called in `chain_service.rs` lines 72–89 invokes `BlockVerifier` (which checks cellbase, bytes, extension, proposals limit, duplicates, merkle root) and `NonContextualBlockTxsVerifier`. Neither checks epoch continuity — that is done by `HeaderVerifier::EpochVerifier` (lines 133–148 of `verification/src/header_verifier.rs`), which is a **context-dependent** verifier requiring the parent block and is not called here. An attacker can therefore craft blocks with arbitrary well-formed epoch numbers that pass `non_contextual_verify`.

When `clean_expired_blocks` (lines 99–110) fires and the expired child is sampled first, `remove_blocks_by_parent` (lines 56–88) performs a BFS that removes **all** children and their descendants from `self.blocks`, `self.parents`, and `self.leaders`. `clean_expired_orphans` in `orphan_broker.rs` lines 134–155 then calls `delete_block`, `remove_header_view`, and `remove_block_status` on every returned block — including non-expired ones. The timer fires every 60 seconds (`chain_service.rs` lines 40–41).

Conversely, if the non-expired child is sampled first, `need_clean` returns `false` and expired blocks accumulate indefinitely. The `InnerPool` uses plain `HashMap` with no enforced size cap, so an attacker inserting many expired blocks under the same leader can cause unbounded memory growth.

## Impact Explanation
**Primary path (expired child sampled first):** Non-expired orphan blocks are permanently deleted from RocksDB and their `block_status` entries removed. When the parent block arrives, the node cannot find these orphans in the pool and must re-request them from peers, causing sync delays and disruption. This matches **Low (501–2000 points): Any other important performance improvements for CKB**, as the node's sync correctness is degraded by a logic error reachable by any unprivileged peer.

**Secondary path (non-expired child sampled first):** Expired blocks are never cleaned. Because `InnerPool` has no enforced size limit, an attacker inserting many expired blocks under a single leader can grow the orphan pool without bound, potentially exhausting node memory and crashing the process. This path approaches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**, but the claim's primary evidence and PoC focus on the sync-disruption path.

## Likelihood Explanation
- Any unprivileged P2P peer can send blocks; no special privilege is required.
- Crafting two structurally valid blocks with the same parent hash but different epoch numbers requires no PoW (PoW is checked only by the context-dependent `HeaderVerifier`, not by `non_contextual_verify`).
- The target parent hash is observable from the sync protocol's `GetBlocks` messages.
- The cleanup timer fires every 60 seconds, giving a reliable trigger window.
- The non-deterministic HashMap sampling gives approximately 50% probability per cleanup cycle that the expired block is chosen first; the attacker can repeat the attack across cycles.

## Recommendation
`need_clean` must inspect **all** direct children of a leader. The subtree should only be purged when every child is expired:

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .map(|map| {
            !map.is_empty()
                && map.values().all(|lonely_block| {
                    lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
                })
        })
        .unwrap_or_default()
}
```

Additionally, consider enforcing a maximum size on `InnerPool` to bound memory growth regardless of cleanup correctness.

## Proof of Concept
```
Setup:
  tip_epoch = 10, EXPIRED_EPOCH = 6

Attacker sends to target node (no valid PoW required; non_contextual_verify passes):
  B1: parent_hash = P, epoch = EpochNumberWithFraction::new(3, 0, 1000)
      → expired: 3 + 6 = 9 < 10
  B2: parent_hash = P, epoch = EpochNumberWithFraction::new(10, 0, 1000)
      → not expired: 10 + 6 = 16 >= 10

Both inserted: blocks[P] = {B1_hash: B1, B2_hash: B2}, leaders = {P}

60 seconds later, clean_expired_orphans fires:
  tip_epoch_number = 10
  clean_expired_blocks(10) → need_clean(P, 10)
  map.iter().next() → B1 (non-deterministic, ~50% probability)
  B1.epoch_number() + 6 = 9 < 10 → need_clean returns true
  remove_blocks_by_parent(P) removes BOTH B1 and B2
  delete_block called on B2; remove_block_status called on B2

Result:
  B2 (non-expired, legitimate) permanently deleted from RocksDB.
  When P arrives, node cannot process B2 and must re-request it.
  Attacker repeats with a new expired block B3 under P each cycle.

Unit test plan:
  Construct OrphanBlockPool, insert two LonelyBlockHash entries under the same
  parent_hash with epoch numbers 3 and 10 respectively.
  Call clean_expired_blocks(10) in a loop; assert that B2 is never returned
  in the cleaned set (i.e., the fix prevents its purge).
```