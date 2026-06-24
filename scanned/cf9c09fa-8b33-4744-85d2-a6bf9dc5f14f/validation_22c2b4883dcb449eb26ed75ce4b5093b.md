The bug is confirmed in the actual code. At line 252 of `util/rich-indexer/src/indexer/remove.rs`, after fetching `row_type`, the second `match` block reads `row_lock` again instead of `row_type`. [1](#0-0) [2](#0-1) 

Audit Report

## Title
Wrong Row Variable in `script_exists_in_output` Causes Premature Type-Script Deletion on PostgreSQL During Block Rollback - (File: `util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the second `match` block at line 252 reads from `row_lock` (the lock-script existence query result) instead of `row_type` (the type-script existence query result). On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds and returns the already-known `false`, so the function returns `Ok(false)` without ever consulting `row_type`. This causes `rollback_block` to delete type-script rows from the `script` table that are still referenced by other outputs, permanently corrupting the rich indexer's relational state.

## Finding Description
`script_exists_in_output` executes two SQL `EXISTS` queries: `row_lock` checks `WHERE lock_script_id = $1` and `row_type` checks `WHERE type_script_id = $1`. After the first query, if the script is not found as a lock script, execution falls through to the second query. However, the second `match` block at line 252 mistakenly decodes `row_lock` again:

```rust
// line 252 — BUG: should be row_type.try_get, not row_lock.try_get
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN`) and returns the already-known `false` value, so the function returns `Ok(false)` without ever reading `row_type`. On SQLite, `try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err` branch correctly reads `row_type.get::<i64, _>(0)` — SQLite is unaffected.

The caller `rollback_block` (lines 29–38) uses the return value to decide whether to delete a script row. Because `script_exists_in_output` incorrectly returns `false` for any script referenced only as a type script, every such script is added to `script_id_list_to_remove` and deleted from the `script` table, even though other outputs still reference it.

## Impact Explanation
This is a concrete instance of **suboptimal/incorrect implementation of CKB state storage mechanism** (Medium, 2001–10000 points). The `script` table is the authoritative source for script metadata in the rich indexer. Premature deletion of type-script rows breaks the relational state and causes `get_cells`, `get_transactions`, and `get_cells_capacity` RPC calls filtering by type script to return empty or incorrect results. The corruption is permanent until the indexer is rebuilt from scratch.

## Likelihood Explanation
Chain reorganizations are a normal, externally triggerable event: any peer presenting a heavier competing chain causes a reorg. PostgreSQL is the recommended production database backend. The bug is deterministic — every reorg that rolls back a block containing outputs with type scripts (virtually every block on mainnet) triggers the incorrect deletion on PostgreSQL. No special privileges or victim mistakes are required.

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

Add an integration test that rolls back a block containing an output whose type script is not shared with any lock script, and asserts that the script row is **not** deleted when other outputs still reference it as a type script.

## Proof of Concept
1. Start a CKB node with the rich indexer configured to use PostgreSQL.
2. Submit and mine a block containing a transaction whose output has a unique type script (not used as any lock script).
3. Trigger a chain reorganization that rolls back that block (e.g., by submitting a heavier fork).
4. `rollback_block` calls `script_exists_in_output(type_script_id, tx)`.
5. The first query (`lock_script_id = $1`) returns `false`; the `match` at line 223 does not early-return.
6. The second query (`type_script_id = $1`) is executed and stored in `row_type`, but the second `match` at line 252 reads `row_lock` again, returning `Ok(false)`.
7. The type script's row is added to `script_id_list_to_remove` and deleted.
8. Subsequent `get_cells` RPC calls filtering by that type script return an empty result, even if the script is re-introduced in a later block.

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

**File:** util/rich-indexer/src/indexer/remove.rs (L237-256)
```rust
    let row_type = sqlx::query(
        r#"
        SELECT EXISTS (
            SELECT 1
            FROM output
            WHERE type_script_id = $1
        )
        "#,
    )
    .bind(script_id)
    .fetch_one(tx.as_mut())
    .await
    .map_err(|err| Error::DB(err.to_string()))?;

    // pg type is BOOLEAN
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```
