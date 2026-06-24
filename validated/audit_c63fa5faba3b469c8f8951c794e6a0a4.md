The bug is confirmed in the code at line 252. The second `match` block uses `row_lock` instead of `row_type`. [1](#0-0) 

Audit Report

## Title
Wrong Row Variable in `script_exists_in_output` Returns Stale Lock-Script Result for Type-Script Check on PostgreSQL — (File: `util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the second `match` block at line 252 reads from `row_lock` (the lock-script query result) instead of `row_type` (the type-script query result) to determine the PostgreSQL return value. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds and returns the stale lock-script boolean, so the type-script existence result (`row_type`) is never consulted. This causes the function to return `false` when a script exists only as a `type_script_id`, leading `rollback_block` to incorrectly delete live scripts from the `script` table during chain reorganizations, corrupting the rich-indexer's relational state.

## Finding Description
`script_exists_in_output` performs two sequential `EXISTS` queries: one for `lock_script_id` (`row_lock`) and one for `type_script_id` (`row_type`). Because PostgreSQL returns `BOOLEAN` and SQLite returns `BIGINT`, the code uses `try_get::<bool, _>(0)` to branch on the backend type.

The first block (lines 223–235) is correct — it reads `row_lock` for both branches:
```rust
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => { if r { return Ok(true); } }
    Err(_) => { if row_lock.get::<i64, _>(0) == 1 { return Ok(true); } }
}
```

The second block (lines 252–256) is wrong — it reads `row_lock` in the `Ok` branch instead of `row_type`:
```rust
match row_lock.try_get::<bool, _>(0) {   // BUG: should be row_type
    Ok(r) => Ok(r),                       // r is the stale lock-script result
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL always returns `BOOLEAN`), so the `Ok(r)` arm is always taken and `row_type` is never read. The function returns the lock-script boolean a second time instead of the type-script boolean.

The caller `rollback_block` (lines 28–39) uses this return value to decide whether to delete a script:
```rust
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
```

When the script is not a lock script (lock query returns `false`) but is still a type script elsewhere (type query returns `true`), the function incorrectly returns `false`, and the script is pushed into `script_id_list_to_remove` and deleted.

On SQLite, `try_get::<bool, _>` fails (SQLite returns `BIGINT`), so the `Err` branch correctly reads `row_type`. The bug is PostgreSQL-only.

## Impact Explanation
This is an incorrect implementation of the CKB rich-indexer state storage mechanism. During any chain reorganization on a PostgreSQL-backed rich-indexer node, type-script records that are still referenced by live outputs are permanently deleted from the `script` table. Subsequent `get_cells`, `get_transactions`, and `get_cells_capacity` RPC calls filtering by those type scripts return incomplete or empty results. The corruption persists until a full re-sync. This matches **Medium (2001–10000 points): Suboptimal/incorrect implementation of CKB state storage mechanism**.

## Likelihood Explanation
Reorgs are a normal part of CKB chain operation requiring no special privilege. Any node operator running the rich-indexer with PostgreSQL (an explicitly documented and supported configuration) is affected on every reorg that rolls back a block containing outputs with type scripts. No attacker capability beyond being a block producer or relayer on a competing chain tip is required.

## Recommendation
Replace `row_lock` with `row_type` in the second match block at line 252:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

## Proof of Concept
1. Configure a CKB node with `[indexer_v2.rich_indexer] db_type = "postgres"`.
2. Sync to a height where at least one output has a `type_script_id` that is **not** also used as a `lock_script_id` by any other live output.
3. Trigger a reorg rolling back that block (e.g., mine a longer competing chain from a fork point before that block).
4. `rollback_block` calls `script_exists_in_output(type_script_id, tx)`.
5. Lock-script query (`row_lock`) returns `false` (script is not a lock script).
6. Type-script query (`row_type`) returns `true` (script is still referenced as a type script).
7. At line 252, `row_lock.try_get::<bool, _>(0)` succeeds on PostgreSQL and returns `false` (stale lock result).
8. Function returns `Ok(false)` — incorrect.
9. Script is added to `script_id_list_to_remove` and deleted from the `script` table.
10. Subsequent RPC queries for cells/transactions filtered by that type script return empty results despite the cells still existing on-chain.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L251-256)
```rust
    // pg type is BOOLEAN
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```
