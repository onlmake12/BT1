### Title
Indexer Sync Loop Exits Prematurely During Reorg When New Chain Is Shorter — (`File: util/indexer-sync/src/lib.rs`)

### Summary

The `try_loop_sync` function in `IndexerSyncService` breaks out of its sync loop when `get_block_by_number(tip_number + 1)` returns `None`, without first verifying that the current indexer tip is still on the canonical chain. During a chain reorganization where the new canonical chain is shorter than the old chain at the moment of sync, the indexer becomes stuck at a stale tip that no longer exists on the main chain, serving incorrect cell and transaction data to all RPC callers until the new chain grows past the old tip height.

### Finding Description

`try_loop_sync` in `util/indexer-sync/src/lib.rs` drives both the basic indexer and the rich indexer. Its logic is:

1. Call `try_catch_up_with_primary()` so the secondary RocksDB view reflects the current canonical chain.
2. Enter a loop: read the indexer's current tip `(tip_number, tip_hash)`.
3. Fetch `get_block_by_number(tip_number + 1)` from the secondary DB.
4. If the block exists and its `parent_hash == tip_hash` → append.
5. If the block exists but `parent_hash != tip_hash` → roll back one block, then loop.
6. **If the block does not exist → `break`.** [1](#0-0) 

Step 6 is the root cause. After `try_catch_up_with_primary()`, the secondary DB reflects the new canonical chain. If a reorg has occurred and the new canonical chain tip height `K` satisfies `K < tip_number + 1` (i.e., the new chain is shorter than the old chain at the time of the call), then `get_block_by_number(tip_number + 1)` returns `None` and the loop exits immediately — without rolling back any blocks.

The `get_block_by_number` helper resolves the block hash via the main-chain index, which after a reorg points to the new chain: [2](#0-1) 

On every subsequent poll or new-block notification, the same path is taken: the indexer tip is still at `tip_number` (old chain), `get_block_by_number(tip_number + 1)` still returns `None` (new chain hasn't grown that far yet), and the loop breaks again. The indexer is stuck.

The `rollback()` implementations for both the basic indexer (`util/indexer/src/indexer.rs`) and the rich indexer (`util/rich-indexer/src/indexer/remove.rs`) correctly roll back one block at a time: [3](#0-2) 

The problem is not in `rollback()` itself but in the loop that drives it: the loop exits before ever calling `rollback()` when the new chain is shorter.

### Impact Explanation

Any application or user querying the CKB indexer RPC (cell queries, transaction queries, live-cell lookups) receives data from the old, now-invalid chain tip. Cells that were spent in the old chain appear live; cells created in the new chain are invisible. This persists until the new chain grows past the old tip height, at which point the mismatch is detected and rollback begins. For deep reorgs, this window can be significant. Applications making decisions based on indexer data (e.g., wallet balance, UTXO selection, contract state) will act on incorrect state.

### Likelihood Explanation

Chain reorganizations are a normal part of CKB's PoW consensus. A reorg where the new canonical chain is temporarily shorter than the old chain (in block count, while having greater total difficulty) is a realistic scenario, especially during network partitions or competitive mining. No privileged access is required; any peer relaying a valid competing chain with sufficient total difficulty can trigger this. The secondary DB's `try_catch_up_with_primary()` call at the top of `try_loop_sync` ensures the secondary DB reflects the new chain before the loop runs, making the race condition deterministic rather than probabilistic. [4](#0-3) 

### Recommendation

When `get_block_by_number(tip_number + 1)` returns `None`, do not break immediately. Instead, verify that the canonical chain's block at `tip_number` has the same hash as the indexer's current tip. If the hashes differ, the indexer tip is on a detached branch and must be rolled back. Replace the `None => { break; }` arm with:

```rust
None => {
    // Check if current tip is still on the canonical chain
    match self.get_block_by_number(tip_number) {
        Some(canonical_block) if canonical_block.hash() == tip_hash => {
            break; // tip is canonical and no next block yet, done
        }
        _ => {
            // tip is on a detached branch; roll back one block and loop
            indexer.rollback().expect("rollback block should be OK");
        }
    }
}
```

This mirrors the correct multi-step unwinding pattern: roll back one block per iteration until the indexer tip re-aligns with the canonical chain, then stop.

### Proof of Concept

1. Start a CKB node with the indexer enabled.
2. Mine a chain to height N (indexer tip = N, hash = H_N).
3. Trigger a reorg: present a competing chain that diverges at height M (M < N) and whose current tip is at height K where M < K < N (new chain is shorter than old chain).
4. The primary DB updates its canonical chain index to the new chain.
5. `try_loop_sync` is called (via new-block notification or poll interval).
6. `try_catch_up_with_primary()` advances the secondary DB to the new chain.
7. `get_block_by_number(N + 1)` returns `None` (new chain tip K < N + 1).
8. Loop breaks. Indexer tip remains at N (old chain hash H_N).
9. Query `get_cells` or `get_transactions` via RPC — results reflect the old chain state.
10. Repeat steps 5–9 on every subsequent poll; the indexer remains stuck until the new chain mines past height N. [5](#0-4)

### Citations

**File:** util/indexer-sync/src/lib.rs (L136-142)
```rust
    fn try_loop_sync<I>(&self, indexer: I)
    where
        I: IndexerSync + Clone + Send + 'static,
    {
        if let Err(e) = self.secondary_db.try_catch_up_with_primary() {
            error!("secondary_db try_catch_up_with_primary error {}", e);
        }
```

**File:** util/indexer-sync/src/lib.rs (L149-182)
```rust
            match indexer.tip() {
                Ok(Some((tip_number, tip_hash))) => {
                    match self.get_block_by_number(tip_number + 1) {
                        Some(block) => {
                            if block.parent_hash() == tip_hash {
                                info!(
                                    "{} append {}, {}",
                                    indexer.get_identity(),
                                    block.number(),
                                    block.hash()
                                );
                                if let Err(e) = indexer.append(&block) {
                                    error!("Failed to append block: {}. Will attempt to retry.", e);
                                }
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
                            }
                        }
                        None => {
                            break;
                        }
                    }
```

**File:** util/indexer-sync/src/lib.rs (L301-304)
```rust
    fn get_block_by_number(&self, block_number: u64) -> Option<core::BlockView> {
        let block_hash = self.secondary_db.get_block_hash(block_number)?;
        self.secondary_db.get_block(&block_hash)
    }
```

**File:** util/rich-indexer/src/indexer/remove.rs (L7-12)
```rust
pub(crate) async fn rollback_block(tx: &mut Transaction<'_, Any>) -> Result<(), Error> {
    let block_id = if let Some(block_id) = query_tip_id(tx).await? {
        block_id
    } else {
        return Ok(());
    };
```
