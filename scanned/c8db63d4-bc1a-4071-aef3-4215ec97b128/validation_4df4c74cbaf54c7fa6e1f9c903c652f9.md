The bug is real and confirmed in the source code. Here is the analysis:

---

### Title
Copy-Paste Error in `script_exists_in_output` Causes Type-Script Rows to Be Incorrectly Deleted on PostgreSQL During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary

`script_exists_in_output` executes two SQL `EXISTS` queries — one against `lock_script_id` and one against `type_script_id` — but the final `match` on line 252 reads from `row_lock` (the first query's result) instead of `row_type` (the second query's result). On PostgreSQL this causes the function to always return `false` for the type-script check, so every type-script ID is unconditionally added to the deletion list during `rollback_block`, even when other outputs still reference it.

### Finding Description

In `script_exists_in_output` [1](#0-0) :

1. `row_lock` is fetched: `SELECT EXISTS (… WHERE lock_script_id = $1)` [2](#0-1) 
2. If `row_lock` is `true`, the function returns `Ok(true)` early — correct. [3](#0-2) 
3. `row_type` is fetched: `SELECT EXISTS (… WHERE type_script_id = $1)` [4](#0-3) 
4. **Line 252** matches on `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. [5](#0-4) 

Because execution only reaches line 252 when `row_lock` was already `false` (the early-return at lines 223–235 consumed the `true` case), `row_lock.try_get::<bool, _>(0)` always yields `Ok(false)` on PostgreSQL. The function therefore always returns `false` for the type-script existence check on PostgreSQL, regardless of the actual database state.

The SQLite path is accidentally correct: `row_lock.try_get::<bool, _>` returns `Err(_)` on SQLite (BIGINT, not BOOLEAN), so the `Err` branch on line 255 falls through to `row_type.get::<i64, _>(0) == 1`, which reads the right variable. [6](#0-5) 

The caller in `rollback_block` uses the return value to decide whether to push a script ID into `script_id_list_to_remove`: [7](#0-6) 

```rust
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
```

Because `script_exists_in_output` always returns `false` for type scripts on PostgreSQL, every type-script ID encountered during rollback is deleted — including those still referenced by outputs that survive the reorg. [8](#0-7) 

### Impact Explanation

After a rollback on a PostgreSQL-backed rich-indexer, the `script` table is missing rows for type scripts that are still referenced by live outputs. Any subsequent `get_cells` or `get_transactions` RPC call that filters by type script will silently return empty or wrong results. The corruption is permanent (no self-healing path) and affects all callers of the indexer API for those scripts.

### Likelihood Explanation

- Reorgs are a normal, unprivileged blockchain event. A single competing block from any miner (including a small miner) is sufficient to trigger a 1-block reorg and invoke `rollback_block`.
- The bug only manifests on PostgreSQL; SQLite deployments are unaffected.
- PostgreSQL is the recommended production backend for the rich-indexer, so production deployments are at risk.
- No special attacker capability is required beyond producing a valid competing block.

### Recommendation

Change line 252 from:

```rust
match row_lock.try_get::<bool, _>(0) {
```

to:

```rust
match row_type.try_get::<bool, _>(0) {
``` [5](#0-4) 

### Proof of Concept

1. Start a CKB node with the rich-indexer backed by PostgreSQL.
2. Index a block containing outputs with type scripts.
3. Trigger a 1-block reorg (mine a competing block at the same height with higher total difficulty).
4. Observe that `rollback_block` is called and `script_exists_in_output` is invoked for each type-script ID.
5. Query the `script` table: type-script rows that are still referenced by outputs in the surviving chain will be absent.
6. Issue a `get_cells` RPC filtered by one of those type scripts: the result will be empty despite live cells existing.

A differential test can confirm this: run rollback with the buggy code vs. the one-line fix and compare `SELECT COUNT(*) FROM script` — the fixed version will retain the correct rows.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L27-39)
```rust
    // remove script
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

**File:** util/rich-indexer/src/indexer/remove.rs (L204-257)
```rust
async fn script_exists_in_output(
    script_id: i64,
    tx: &mut Transaction<'_, Any>,
) -> Result<bool, Error> {
    let row_lock = sqlx::query(
        r#"
        SELECT EXISTS (
            SELECT 1
            FROM output
            WHERE lock_script_id = $1
        )
        "#,
    )
    .bind(script_id)
    .fetch_one(tx.as_mut())
    .await
    .map_err(|err| Error::DB(err.to_string()))?;

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
}
```
