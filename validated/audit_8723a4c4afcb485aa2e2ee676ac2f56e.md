The code at line 252 confirms the exact bug described. `row_lock.try_get::<bool, _>(0)` is used instead of `row_type.try_get::<bool, _>(0)` in the second match block. [1](#0-0) 

Audit Report

## Title
Wrong Row Variable in `script_exists_in_output` Returns Stale Lock-Script Result for Type-Script Check on PostgreSQL — (File: `util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the second `match` block at line 252 reads from `row_lock` (the lock-script query result) instead of `row_type` (the type-script query result). On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds and returns the stale lock-script boolean, so the type-script existence check always returns the wrong value. During `rollback_block`, this causes type scripts still referenced by live outputs to be incorrectly added to `script_id_list_to_remove` and deleted from the `script` table, permanently corrupting the rich-indexer's PostgreSQL database.

## Finding Description
`script_exists_in_output` performs two sequential `EXISTS` queries: one against `lock_script_id` (stored in `row_lock`) and one against `type_script_id` (stored in `row_type`). Because PostgreSQL returns `BOOLEAN` and SQLite returns `BIGINT`, the code uses `try_get::<bool, _>(0)` to branch on backend type.

The first block (lines 223–235) is correct — it reads `row_lock` for the lock-script result: [2](#0-1) 

The second block (lines 251–256) is the bug — it reads `row_lock` again instead of `row_type`: [1](#0-0) 

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL always returns `BOOLEAN`), so `Ok(r)` is always taken — but `r` is the lock-script boolean from the first query, not the type-script boolean from `row_type`. `row_type` is never read on PostgreSQL. On SQLite, `try_get::<bool, _>` fails, so the `Err` branch correctly reads `row_type.get::<i64, _>(0)`. The bug is PostgreSQL-only.

The caller at lines 33–37 uses the return value to decide deletion: [3](#0-2) 

When the lock-script query returned `false` (script is not a lock script) and the type-script query returns `true` (script is still referenced as a type script), the function incorrectly returns `Ok(false)` on PostgreSQL, causing the live script to be pushed into `script_id_list_to_remove` and deleted.

## Impact Explanation
This is a suboptimal/incorrect implementation of the CKB state storage mechanism (rich-indexer). On PostgreSQL, any chain reorg that rolls back a block containing outputs with type scripts will silently delete those type-script records from the `script` table even when they are still referenced by other live outputs. This permanently corrupts the indexer's relational state. Subsequent `get_cells`, `get_transactions`, and `get_cells_capacity` RPC calls filtering by the affected type script return incomplete or empty results. The corruption persists until a full re-sync. This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation
Reorgs are a normal part of CKB chain operation requiring no special privilege. The only preconditions are: (1) the node operator uses the rich-indexer with PostgreSQL (non-default but explicitly documented and supported), and (2) a reorg rolls back a block containing outputs with type scripts (extremely common). No attacker capability beyond being a block producer or relayer on a competing chain tip is required. The bug triggers on every qualifying reorg.

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
2. Sync to a height where at least one output has a non-null `type_script_id` that is **not** also used as a `lock_script_id` by any other live output.
3. Trigger a reorg rolling back that block (e.g., mine a longer competing chain from a fork point before that block).
4. `rollback_block` calls `script_exists_in_output(type_script_id, tx)`.
5. Lock-script query (`row_lock`) returns `false` (script is not a lock script).
6. Type-script query (`row_type`) returns `true` (script is still referenced).
7. Line 252: `row_lock.try_get::<bool, _>(0)` succeeds on PostgreSQL, returns `false` (stale lock result).
8. Function returns `Ok(false)` — incorrect.
9. Script is pushed to `script_id_list_to_remove` and deleted from `script` table.
10. Subsequent RPC queries for cells/transactions filtered by that type script return empty results despite the cells still existing on-chain.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L33-37)
```rust
        if let Some(type_script_id) = type_script_id
            && !script_exists_in_output(type_script_id, tx).await?
        {
            script_id_list_to_remove.push(type_script_id);
        }
```

**File:** util/rich-indexer/src/indexer/remove.rs (L222-235)
```rust
    // pg type is BOOLEAN
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => {
            if r {
                return Ok(true);
            }
        }
        Err(_) => {
            // sqlite type is BIGINT
            if row_lock.get::<i64, _>(0) == 1 {
                return Ok(true);
            }
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
