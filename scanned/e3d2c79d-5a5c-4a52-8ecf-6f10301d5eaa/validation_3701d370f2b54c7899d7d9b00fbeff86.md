### Title
Wrong Row Variable Used for DB-Type Detection Causes Incorrect Script Deletion During Reorg — (File: `util/rich-indexer/src/indexer/remove.rs`)

### Summary

In `script_exists_in_output`, the second `match` block at line 252 reads from `row_lock` (the result of the **lock-script** query) instead of `row_type` (the result of the **type-script** query) to determine the PostgreSQL return type. On PostgreSQL, this causes the function to return the lock-script existence result a second time instead of the type-script existence result, making it return `false` when a script exists only as a `type_script_id`. During block rollback (reorg), this causes live scripts to be incorrectly deleted from the `script` table, corrupting the rich-indexer database.

### Finding Description

`script_exists_in_output` is a two-step existence check:

1. Query `row_lock` — does `script_id` appear as any `lock_script_id` in `output`?
2. If not, query `row_type` — does `script_id` appear as any `type_script_id` in `output`?

Because PostgreSQL's `EXISTS(...)` returns `BOOLEAN` while SQLite returns `BIGINT`, the code uses `try_get::<bool, _>(0)` to detect which backend is in use and branch accordingly.

The first block (lines 223–235) is correct:

```rust
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => { if r { return Ok(true); } }
    Err(_) => { if row_lock.get::<i64, _>(0) == 1 { return Ok(true); } }
}
```

The second block (lines 252–256) is **wrong**:

```rust
// pg type is BOOLEAN
match row_lock.try_get::<bool, _>(0) {   // <-- should be row_type
    Ok(r) => Ok(r),                       // <-- r is from row_LOCK, not row_TYPE
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

`row_lock` is reused instead of `row_type`. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL always returns `BOOLEAN`), so the `Ok(r)` arm is always taken — but `r` is the stale lock-script result, not the type-script result. The `row_type` value is never read on PostgreSQL.

On SQLite, `try_get::<bool, _>` fails (SQLite returns `BIGINT`), so the `Err` branch correctly reads `row_type`. **The bug is PostgreSQL-only.** [1](#0-0) 

The caller `rollback_block` uses the return value to decide whether to delete a script:

```rust
if !script_exists_in_output(lock_script_id, tx).await? {
    script_id_list_to_remove.push(lock_script_id);
}
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
``` [2](#0-1) 

When `script_exists_in_output` incorrectly returns `false` for a script that is still referenced as a `type_script_id`, that script is pushed into `script_id_list_to_remove` and deleted. [3](#0-2) 

### Impact Explanation

On a CKB node running the rich-indexer with a PostgreSQL backend, any chain reorganization (reorg) that rolls back a block containing outputs with type scripts will silently delete those type-script records from the `script` table — even if those scripts are still referenced by other live outputs. This corrupts the indexer's relational state: subsequent `get_cells`, `get_transactions`, and `get_cells_capacity` RPC calls that filter by type script will return incomplete or empty results for affected scripts. The corruption is permanent until the indexer is fully re-synced.

### Likelihood Explanation

Reorgs are a normal part of CKB chain operation and can be triggered by any block relayer or miner producing a competing chain tip. No special privilege is required. Any node operator running the rich-indexer with PostgreSQL (the non-default but explicitly documented and supported configuration) is affected on every reorg that touches outputs with type scripts.

### Recommendation

Replace `row_lock` with `row_type` in the second match block:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [1](#0-0) 

### Proof of Concept

1. Configure a CKB node with `[indexer_v2.rich_indexer] db_type = "postgres"`.
2. Sync the node to a height where at least one output has a non-null `type_script_id` that is **not** also used as a `lock_script_id` by any other live output.
3. Trigger a reorg that rolls back that block (e.g., by mining a longer competing chain from a fork point before that block).
4. `rollback_block` is called → `script_exists_in_output(type_script_id, tx)` is called.
5. The lock-script query (`row_lock`) returns `false` (script is not a lock script).
6. The type-script query (`row_type`) returns `true` (script is still a type script elsewhere).
7. At line 252, `row_lock.try_get::<bool, _>(0)` succeeds on PostgreSQL and returns `false` (the stale lock-script result).
8. `script_exists_in_output` returns `Ok(false)` — wrong.
9. The script is added to `script_id_list_to_remove` and deleted from the `script` table.
10. Subsequent RPC queries for cells/transactions filtered by that type script return empty results despite the cells still existing on-chain. [4](#0-3)

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
