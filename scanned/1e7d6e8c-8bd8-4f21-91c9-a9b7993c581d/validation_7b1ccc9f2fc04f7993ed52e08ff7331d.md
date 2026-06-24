The code at line 252 is confirmed exactly as described. `row_lock.try_get::<bool, _>(0)` is used in the second `match` block instead of `row_type.try_get::<bool, _>(0)`.

Audit Report

## Title
Wrong Variable Reference in `script_exists_in_output()` Causes Premature Script Deletion During Reorg — (File: util/rich-indexer/src/indexer/remove.rs)

## Summary
`script_exists_in_output` in `util/rich-indexer/src/indexer/remove.rs` contains a copy-paste error at line 252: the second `match` block reads `row_lock` (the lock-script query result) instead of `row_type` (the type-script query result). On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds, so `row_type` is never consulted and the function returns the lock-script existence result for both checks. This causes `rollback_block` to incorrectly delete script rows from the `script` table that are still referenced by surviving outputs as type scripts, silently corrupting the rich-indexer database on every reorg involving type-scripted cells.

## Finding Description
In `script_exists_in_output` (lines 204–257), the function executes two SQL `EXISTS` queries: one for `lock_script_id` (result stored in `row_lock`) and one for `type_script_id` (result stored in `row_type`). The first `match` block at line 223 correctly reads `row_lock`. However, the second `match` block at line 252 also reads `row_lock` instead of `row_type`:

```rust
// line 252 — BUG: should be row_type.try_get::<bool, _>(0)
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `SELECT EXISTS(...)` returns a `BOOLEAN`, so `try_get::<bool, _>(0)` on `row_lock` always succeeds (`Ok` arm taken), and the function returns the lock-script result a second time — `row_type` is never read. On SQLite, `SELECT EXISTS(...)` returns `BIGINT`, so `try_get::<bool, _>` fails and the `Err` arm correctly falls through to `row_type.get::<i64, _>(0)`, making SQLite unaffected.

`rollback_block` (lines 29–38) calls `script_exists_in_output` for both `lock_script_id` and `type_script_id` of each rolled-back output, and pushes any script for which the function returns `false` into `script_id_list_to_remove`, which is then deleted via `remove_batch_by_blobs("script", ...)`. Because the function returns the lock-script result for the type-script check on PostgreSQL, any script used only as a type script (not simultaneously as a lock script in any surviving output) is incorrectly reported as absent and deleted from the `script` table, even though live outputs still hold a foreign-key reference to it.

## Impact Explanation
This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**. The rich-indexer is CKB's state storage and indexing layer. Silent deletion of referenced script rows corrupts the rich-indexer database on PostgreSQL: subsequent RPC queries (`get_cells`, `get_transactions`) that join on `script_id` return incomplete or missing results for any cell whose type script was incorrectly purged. The corruption is permanent and accumulates across reorgs.

## Likelihood Explanation
Chain reorganizations are routine and externally triggerable by any peer relaying a competing chain of sufficient work — no special privilege is required. Type scripts are ubiquitous on CKB (UDT, NFT, DAO, etc.). Any PostgreSQL-backed rich-indexer node will silently corrupt its database on every reorg involving type-scripted cells. The condition is deterministic and repeatable.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // ← was row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

## Proof of Concept
1. Set up a PostgreSQL-backed rich-indexer node.
2. Index a block containing an output with a type script whose `script_id` is **not** used as a lock script in any other surviving output.
3. Trigger a chain reorganization that rolls back that block (e.g., by feeding a competing chain of greater work).
4. `rollback_block` calls `script_exists_in_output(type_script_id, tx)`. On PostgreSQL, the function returns the result of the lock-script `EXISTS` query (which is `false` since no output uses this ID as a lock script), causing the type script row to be added to `script_id_list_to_remove` and deleted.
5. Query `get_cells` or `get_transactions` for cells referencing that type script — results will be missing or incomplete, confirming database corruption. [1](#0-0) [2](#0-1)

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L29-39)
```rust
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
