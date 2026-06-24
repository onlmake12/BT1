The code at line 252 confirms the bug exactly as described. [1](#0-0) 

`row_lock.try_get::<bool, _>(0)` is used instead of `row_type.try_get::<bool, _>(0)` at line 252, while `row_type` was correctly fetched at lines 237–249. [2](#0-1) 

On PostgreSQL, `try_get::<bool, _>(0)` on `row_lock` always succeeds (PostgreSQL returns `BOOLEAN`), but execution only reaches line 252 when `row_lock` was `false` (the `true` case returned early at line 226), so this branch always returns `Ok(false)`, never consulting `row_type`. [3](#0-2) 

The `rollback_block` function at lines 33–37 then unconditionally adds every type script ID to `script_id_list_to_remove` and deletes them. [4](#0-3) 

---

Audit Report

## Title
Copy-paste bug in `script_exists_in_output` causes type scripts to be incorrectly deleted during block rollback on PostgreSQL — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the final `match` at line 252 re-reads `row_lock` instead of `row_type`. On PostgreSQL, this means the type-script existence check always returns `false`, causing type scripts still referenced by surviving outputs to be unconditionally deleted from the `script` table during any block rollback. The resulting database corruption is permanent until the indexer is rebuilt.

## Finding Description
`script_exists_in_output` executes two SQL `EXISTS` queries: one binding `lock_script_id` (result in `row_lock`, lines 208–220) and one binding `type_script_id` (result in `row_type`, lines 237–249). The first `match` at line 223 correctly evaluates `row_lock` and returns `Ok(true)` early if the lock query is positive. However, the second `match` at line 252 again evaluates `row_lock` instead of `row_type`:

```rust
// line 252 — BUG: should be row_type.try_get::<bool, _>(0)
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),                               // always false on PG
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1), // correct on SQLite
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL returns `BOOLEAN`). Execution only reaches line 252 when `row_lock` was `false` (the `true` case triggered the early return at line 226), so this branch always returns `Ok(false)`. The `row_type` result is never consulted. Consequently, `rollback_block` (lines 29–38) adds every type script ID to `script_id_list_to_remove` regardless of whether surviving outputs still reference it, and `remove_batch_by_blobs("script", ...)` deletes those rows permanently.

On SQLite, `try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err(_)` arm runs and correctly reads `row_type.get::<i64, _>(0) == 1`. SQLite is unaffected.

## Impact Explanation
This is a suboptimal (incorrect) implementation of the CKB state storage mechanism (rich indexer). After any rollback on a PostgreSQL-backed node, the `script` table loses rows for type scripts still referenced by surviving outputs. Subsequent `get_cells` RPC calls join on `script.id`; with the row absent, those cells are invisible in query results. The corruption is permanent until the indexer is fully rebuilt. This matches the allowed impact: **Medium (2001–10000 points) — Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation
Any chain reorganization (reorg) on a PostgreSQL-backed rich indexer node triggers `rollback_block` and thus this bug. Short 1-block reorgs occur naturally during normal network operation and require no special privileges or attacker capability to trigger. An attacker can also deliberately induce reorgs by mining a competing tip. No victim mistakes or unrealistic assumptions are required.

## Recommendation
Change line 252 from:
```rust
match row_lock.try_get::<bool, _>(0) {
```
to:
```rust
match row_type.try_get::<bool, _>(0) {
```

## Proof of Concept
1. Configure a CKB rich indexer with a PostgreSQL backend.
2. Append a block containing a transaction with at least one output that has a type script (but whose type script is not used as a lock script in any surviving output).
3. Verify the type script row exists in the `script` table: `SELECT * FROM script WHERE id = <type_script_id>`.
4. Trigger a 1-block reorg (append a competing block at the same height), causing `rollback_block` to execute.
5. Query `SELECT * FROM script WHERE id = <type_script_id>` — the row is absent despite surviving outputs still referencing it, confirming the invariant violation. Any `get_cells` call filtering by that type script will return empty results.

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

**File:** util/rich-indexer/src/indexer/remove.rs (L223-235)
```rust
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

**File:** util/rich-indexer/src/indexer/remove.rs (L237-249)
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
```

**File:** util/rich-indexer/src/indexer/remove.rs (L252-256)
```rust
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```
