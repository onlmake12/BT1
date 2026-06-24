The code at line 252 is confirmed exactly as described in the claim.

Audit Report

## Title
Wrong Row Variable in `script_exists_in_output` Causes Type-Script Rows Deleted on PostgreSQL During Reorg - (File: `util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the final `match` block at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this causes the function to return `false` for any script referenced only as a type script, leading `rollback_block` to permanently delete those script rows from the `script` table during every chain reorganization.

## Finding Description
`script_exists_in_output` executes two SQL `EXISTS` queries: `row_lock` (checks `lock_script_id = $1`) and `row_type` (checks `type_script_id = $1`). After the first query, if the script is not found as a lock script, execution continues to the second query. However, the second `match` block at line 252 mistakenly decodes `row_lock` again instead of `row_type`:

```rust
// line 252 — BUG: row_lock instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),          // returns false (already-known lock result)
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds and returns the already-known `false` (the script was not found as a lock script), so the function returns `Ok(false)` without ever consulting `row_type`. On SQLite, `try_get::<bool, _>` fails (SQLite returns `BIGINT`), so the `Err` branch correctly reads `row_type` — SQLite is unaffected.

The caller `rollback_block` (lines 29–38) uses this return value to decide whether to delete a script row. Because `script_exists_in_output` incorrectly returns `false` for any script referenced only as a type script, every such script is pushed to `script_id_list_to_remove` and deleted from the `script` table, even when other outputs still reference it.

## Impact Explanation
This is a correctness bug in CKB's rich-indexer state storage mechanism. The `script` table is the authoritative source for script metadata. Premature deletion of type-script rows corrupts the relational state permanently (until a full rebuild), causing `get_cells`, `get_transactions`, and `get_cells_capacity` RPC calls that filter by type script to return incorrect or empty results. This matches **Medium (2001–10000 points): Suboptimal/incorrect implementation of CKB state storage mechanism**.

## Likelihood Explanation
Chain reorganizations are a normal, externally triggerable event requiring no special privileges — any peer presenting a heavier competing chain causes a reorg. PostgreSQL is the recommended production backend. The bug is deterministic: every reorg rolling back a block containing outputs with type scripts (virtually every mainnet block) triggers the incorrect deletion on PostgreSQL.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // corrected
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Add an integration test that rolls back a block containing an output whose type script is not shared with any lock script, and asserts the script row is **not** deleted when other outputs still reference it as a type script.

## Proof of Concept
1. Start a CKB node with the rich indexer configured to use PostgreSQL.
2. Mine a block containing a transaction output with a unique type script (not used as any lock script).
3. Trigger a chain reorganization that rolls back that block.
4. `rollback_block` calls `script_exists_in_output(type_script_id, tx)`.
5. The first query (`lock_script_id = $1`) returns `false`; execution falls through to the second query.
6. The second query (`type_script_id = $1`) is stored in `row_type`, but the second `match` reads `row_lock` again, returning `Ok(false)`.
7. The type script's row is added to `script_id_list_to_remove` and deleted from the `script` table.
8. Subsequent `get_cells` RPC calls filtering by that type script return empty results even after the script is re-introduced in a later block. [1](#0-0) [2](#0-1)

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L29-38)
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
