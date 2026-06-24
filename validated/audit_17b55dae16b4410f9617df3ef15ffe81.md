The bug is confirmed exactly as described. At line 252, `row_lock.try_get::<bool, _>(0)` is used instead of `row_type.try_get::<bool, _>(0)`. [1](#0-0) 

On PostgreSQL, `row_lock` holds a SQL `BOOLEAN` that is already known to be `false` (the early-return guard at lines 223–235 ensures execution only reaches line 252 when `row_lock` is false). So `row_lock.try_get::<bool, _>(0)` succeeds with `Ok(false)`, and `row_type` is never consulted. [2](#0-1) 

On SQLite, `EXISTS` returns `BIGINT`, so `row_lock.try_get::<bool, _>(0)` fails, falling into the `Err(_)` arm which correctly reads `row_type`. SQLite is unaffected.

All rollback tests use only SQLite (`connect_sqlite(MEMORY_DB)`), so this PostgreSQL-specific path is never exercised. [3](#0-2) [4](#0-3) 

The caller unconditionally pushes the type-script ID into the deletion list when the function returns `false`: [5](#0-4) 

---

Audit Report

## Title
Wrong Row Variable in `script_exists_in_output` Causes Type-Script Deletion on PostgreSQL During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
A copy-paste error at line 252 of `script_exists_in_output` reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)` in the final match. On PostgreSQL, `row_lock` is a `BOOLEAN` already known to be `false` at that point, so the function always returns `Ok(false)` for any script that is referenced only as a type-script. This causes every such type-script to be permanently deleted from the `script` table during every rollback on a PostgreSQL-backed rich-indexer node, corrupting all subsequent type-script-based indexer queries.

## Finding Description
`script_exists_in_output` executes two `SELECT EXISTS` queries: `row_lock` (checks `lock_script_id`) and `row_type` (checks `type_script_id`). The early-return guard at lines 223–235 returns `Ok(true)` immediately if `row_lock` is true. If execution reaches line 252, `row_lock` is definitively `false`. The final match at line 252 should evaluate `row_type` but instead re-evaluates `row_lock`:

```rust
// line 252 — BUG
match row_lock.try_get::<bool, _>(0) {   // should be row_type
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `EXISTS` returns SQL `BOOLEAN`. `row_lock.try_get::<bool, _>(0)` succeeds with `Ok(false)` (the value is already false). The function returns `Ok(false)` regardless of `row_type`'s content. On SQLite, `EXISTS` returns `BIGINT`, so `try_get::<bool, _>` fails, the `Err(_)` arm is taken, and `row_type` is read correctly — SQLite is not affected.

The caller in `rollback_block` pushes the type-script ID into `script_id_list_to_remove` whenever the function returns `false`, then deletes all collected IDs from the `script` table via `remove_batch_by_blobs`. On PostgreSQL, every type-script that is not simultaneously a lock-script is unconditionally deleted during every rollback, even when surviving outputs still reference it.

## Impact Explanation
This is a correctness defect in the CKB rich-indexer's state storage mechanism. After deletion, surviving outputs whose `type_script_id` foreign key pointed to the now-deleted row are left with dangling references. All indexer query paths that join on `type_script_id` — `get_cells`, `get_cells_capacity`, `get_transactions` — return empty or incorrect results for those scripts. The corruption is persistent (committed inside the same DB transaction as the rollback) and accumulates across every subsequent reorg. This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation
Chain reorganizations are a routine part of CKB operation and require no attacker. Any natural reorg on a PostgreSQL-backed rich-indexer node triggers the bug. No special privileges or attacker capability are required. The existing rollback test suite uses only SQLite and does not exercise the PostgreSQL code path, so the bug is not caught by CI.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // ← fix
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Add a PostgreSQL-backed rollback integration test that indexes two blocks sharing a type script, rolls back the second block, and asserts the shared script row still exists in the `script` table.

## Proof of Concept
1. Start a PostgreSQL-backed rich-indexer node.
2. Index block A containing output O1 with `type_script_id = S` (S is not a lock-script).
3. Index block B (tip) containing output O2 with the same `type_script_id = S`.
4. Trigger a rollback of block B (natural reorg).
5. `rollback_block` removes block B's outputs, then calls `script_exists_in_output(S, tx)`.
6. `row_lock` query: S is not a `lock_script_id` in any surviving output → `false`.
7. Early-return guard not taken (row_lock is false).
8. `row_type` query: S IS a `type_script_id` (O1 still exists) → `true`.
9. Line 252: `row_lock.try_get::<bool, _>(0)` → `Ok(false)` (PostgreSQL BOOLEAN, value is false).
10. Function returns `Ok(false)` — script incorrectly flagged for deletion.
11. Script row S is deleted from the `script` table.
12. Query `get_cells` with type-script filter for S → empty result, even though O1 still exists.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L33-39)
```rust
        if let Some(type_script_id) = type_script_id
            && !script_exists_in_output(type_script_id, tx).await?
        {
            script_id_list_to_remove.push(type_script_id);
        }
    }
    remove_batch_by_blobs("script", "id", &script_id_list_to_remove, tx).await?;
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

**File:** util/rich-indexer/src/tests/rollback.rs (L7-7)
```rust
    let storage = connect_sqlite(MEMORY_DB).await;
```

**File:** util/rich-indexer/src/tests/rollback.rs (L63-63)
```rust
    let storage = connect_sqlite(MEMORY_DB).await;
```
