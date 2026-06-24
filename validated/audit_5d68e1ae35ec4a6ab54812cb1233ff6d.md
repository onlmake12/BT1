Audit Report

## Title
Indexer Sync Loop Exits Without Rollback When Reorg Produces a Shorter Chain — (`File: util/indexer-sync/src/lib.rs`)

## Summary
In `try_loop_sync`, when `get_block_by_number(tip_number + 1)` returns `None`, the loop unconditionally breaks without verifying that the indexer's current tip hash still matches the canonical chain at `tip_number`. During a chain reorganization where the new canonical chain tip height `K` is less than the indexer's recorded tip height `N`, the indexer becomes permanently stuck at the stale tip until the new chain mines past height `N`, serving incorrect cell and transaction data to all RPC callers throughout that window.

## Finding Description
The root cause is at lines 179–181 of `util/indexer-sync/src/lib.rs`:

```rust
None => {
    break;
}
```

The full loop in `try_loop_sync` (lines 143–199) reads the indexer tip `(tip_number, tip_hash)` and calls `self.get_block_by_number(tip_number + 1)`. The helper at lines 301–304 resolves the block hash via the canonical chain index in the secondary DB:

```rust
fn get_block_by_number(&self, block_number: u64) -> Option<core::BlockView> {
    let block_hash = self.secondary_db.get_block_hash(block_number)?;
    self.secondary_db.get_block(&block_hash)
}
```

After `try_catch_up_with_primary()` at line 140 updates the secondary DB to reflect the new canonical chain, if the new chain tip height `K < tip_number + 1`, `get_block_by_number(tip_number + 1)` returns `None` and the loop exits immediately — without ever calling `rollback()`. The indexer tip remains at `tip_number` with the old chain's hash.

The rollback path at lines 163–176 is only reachable when `get_block_by_number(tip_number + 1)` returns `Some(block)` with a mismatched `parent_hash`. When the new chain is shorter than the old indexer tip, this branch is structurally unreachable: `get_block_by_number(tip_number + 1)` returns `None` on every subsequent invocation until the new chain mines past height `tip_number`, so the loop always exits at line 180 without rollback.

## Impact Explanation
The indexer — which backs all cell queries, transaction queries, and live-cell lookups via RPC — serves data from the old, now-invalid chain tip for the entire window between the reorg and the new chain growing past the old tip height. Cells spent on the old chain appear live; cells created on the new chain are invisible. This is a concrete, reproducible failure of the CKB state storage mechanism, matching the allowed bounty impact: **Suboptimal implementation of CKB state storage mechanism (Medium, 2001–10000 points)**.

## Likelihood Explanation
Chain reorganizations are a normal part of CKB's PoW consensus. A reorg where the new canonical chain is temporarily shorter in block count (while having greater total difficulty) is realistic due to block difficulty variance within an epoch. No privileged access is required; any peer relaying a valid competing chain with sufficient total difficulty triggers this. The `try_catch_up_with_primary()` call at line 140 ensures the secondary DB reflects the new chain before the loop runs, making the condition deterministic: if the new chain tip height `K < tip_number`, the bug fires on every invocation until the new chain catches up.

## Recommendation
Replace the `None => { break; }` arm at lines 179–181 with a canonical-tip check before breaking:

```rust
None => {
    match self.get_block_by_number(tip_number) {
        Some(canonical_block) if canonical_block.hash() == tip_hash => {
            break; // tip is canonical and no next block yet, done
        }
        _ => {
            info!(
                "{} rollback {}, {}",
                indexer.get_identity(),
                tip_number,
                tip_hash
            );
            indexer.rollback().expect("rollback block should be OK");
            if let Err(e) = self.secondary_db.try_catch_up_with_primary() {
                error!(
                    "after rollback, secondary_db try_catch_up_with_primary error {}",
                    e
                );
            }
        }
    }
}
```

This mirrors the existing rollback pattern at lines 163–176 and ensures the indexer unwinds to the fork point before stopping.

## Proof of Concept
1. Start a CKB node with the indexer enabled.
2. Mine a chain to height N (indexer tip = N, hash = H_N on the old chain).
3. Trigger a reorg: present a competing chain diverging at height M (M < N) whose current tip is at height K where M < K < N (new chain is shorter but has greater total difficulty).
4. The primary DB updates its canonical chain index to the new chain.
5. `try_loop_sync` is called (via new-block notification or poll interval).
6. `try_catch_up_with_primary()` at line 140 advances the secondary DB to the new chain.
7. `get_block_by_number(N + 1)` returns `None` (new chain tip K < N + 1).
8. Loop breaks at line 180. Indexer tip remains at N with old chain hash H_N.
9. Query `get_cells` or `get_transactions` via RPC — results reflect the old chain state.
10. Repeat steps 5–9 on every subsequent poll; the indexer remains stuck until the new chain mines past height N, at which point the `parent_hash` mismatch at line 153 is detected and rollback begins. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** util/indexer-sync/src/lib.rs (L140-142)
```rust
        if let Err(e) = self.secondary_db.try_catch_up_with_primary() {
            error!("secondary_db try_catch_up_with_primary error {}", e);
        }
```

**File:** util/indexer-sync/src/lib.rs (L163-176)
```rust
                            } else {
                                info!(
                                    "{} rollback {}, {}",
                                    indexer.get_identity(),
                                    tip_number,
                                    tip_hash
                                );
                                indexer.rollback().expect("rollback block should be OK");
                                if let Err(e) = self.secondary_db.try_catch_up_with_primary() {
                                    error!(
                                        "after rollback, secondary_db try_catch_up_with_primary error {}",
                                        e
                                    );
                                }
```

**File:** util/indexer-sync/src/lib.rs (L179-181)
```rust
                        None => {
                            break;
                        }
```

**File:** util/indexer-sync/src/lib.rs (L301-304)
```rust
    fn get_block_by_number(&self, block_number: u64) -> Option<core::BlockView> {
        let block_hash = self.secondary_db.get_block_hash(block_number)?;
        self.secondary_db.get_block(&block_hash)
    }
```
