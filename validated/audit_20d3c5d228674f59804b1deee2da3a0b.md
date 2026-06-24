The bug at line 252 is confirmed in the actual source code.

Audit Report

## Title
`script_exists_in_output` Re-Checks `row_lock` Instead of `row_type`, Silently Skipping the Type-Script Existence Check on PostgreSQL During Block Rollback — (File: util/rich-indexer/src/indexer/remove.rs)

## Summary
In `script_exists_in_output`, the final `match` block at line 252 reads `row_lock` a second time instead of `row_type`. On PostgreSQL, where `try_get::<bool, _>(0)` succeeds, the type-script query result is fetched but never evaluated, causing the function to return the lock-script result for both checks. During `rollback_block`, any script referenced exclusively as a `type_script_id` is incorrectly reported as absent and deleted from the `script` table, permanently corrupting the rich indexer's relational state.

## Finding Description
`script_exists_in_output` (lines 204–257) executes two `SELECT EXISTS` queries — one against `lock_script_id`, one against `type_script_id` — and should return `true` if either matches. The first `match` on `row_lock` (line 223) correctly early-returns `Ok(true)` when the lock query finds a row. After fetching `row_type` (line 237), the second `match` at line 252 mistakenly re-reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL the column type is `BOOLEAN`, so `try_get::<bool, _>` always succeeds and the `Ok(r)` branch is taken — returning the stale lock-script boolean. `row_type` is fetched but its value is never read. On SQLite the column type is `BIGINT`, `try_get::<bool, _>` fails, the `Err(_)` branch correctly uses `row_type.get::<i64, _>(0)`, so SQLite is unaffected. The caller in `rollback_block` (lines 28–39) pushes any script ID for which `script_exists_in_output` returns `false` into `script_id_list_to_remove`, then deletes all of them from the `script` table via `remove_batch_by_blobs`.

## Impact Explanation
This is a concrete corruption of the CKB rich indexer's state storage mechanism, matching the **Medium (2001–10000 points)** bounty class: *Suboptimal implementation of CKB state storage mechanism*. After any rollback involving outputs with type scripts, the `script` table loses rows that are still actively referenced by surviving outputs. Subsequent RPC calls (`get_cells`, `get_transactions`, `get_cells_capacity`) filtering by those type scripts return empty or incorrect results. The corruption is permanent until a full re-index is performed.

## Likelihood Explanation
The trigger is a chain reorganization, a routine and expected event in CKB. No attacker capability is required — any valid competing chain with more accumulated work causes the node to call `indexer.rollback()` (line 170 of `util/indexer-sync/src/lib.rs`). Type-scripted outputs (DAO deposits, UDTs, NFTs) are extremely common, so virtually every reorg on a PostgreSQL-backed rich indexer node triggers the bug.

## Recommendation
Replace `row_lock` with `row_type` in the final `match` block of `script_exists_in_output` at line 252:

```rust
// Before (buggy) — util/rich-indexer/src/indexer/remove.rs line 252
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}

// After (correct)
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Add a PostgreSQL-backed integration test for `rollback_block` asserting that a script used exclusively as a `type_script_id` in surviving outputs is not deleted after rollback.

## Proof of Concept
1. Start a rich indexer with a PostgreSQL backend.
2. Append block 1 containing an output with `lock_script_id = A`, `type_script_id = B` (script B appears nowhere as a lock script).
3. Append block 2 spending that output and creating a new output with `lock_script_id = C`, `type_script_id = B`.
4. Trigger rollback of block 2 (simulate a reorg).
5. **Expected:** `script_exists_in_output(B)` returns `true` (B is still referenced as `type_script_id` in the block-1 output); script B is not deleted.
6. **Actual (PostgreSQL):** The second `match` evaluates `row_lock.try_get::<bool, _>(0)` → `Ok(false)` (lock query found nothing for B); function returns `false`; script B is added to `script_id_list_to_remove` and deleted. Subsequent `get_cells` filtered by script B returns empty results despite live outputs referencing it. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L28-39)
```rust
    let mut script_id_list_to_remove = Vec::new();
    for (_, lock_script_id, type_script_id) in output_lock_type_list {
        if !script_exists_in_output(lock_script_id, tx).await? {
            script_id_list_to_remove.push(lock_script_id);
        }
        if let Some(type_script_id) = type_script_id
            && !script_exists_in_output(type_script_id, tx).await?
        {
            script_id_list_to_remove.push(type_script_id);
        }
    }
    remove_batch_by_blobs("script", "id", &script_id_list_to_remove, tx).await?;
```

**File:** util/rich-indexer/src/indexer/remove.rs (L251-256)
```rust
    // pg type is BOOLEAN
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```

**File:** util/indexer-sync/src/lib.rs (L170-170)
```rust
                                indexer.rollback().expect("rollback block should be OK");
```
