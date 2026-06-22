### Title
`script_exists_in_output` Always Checks `row_lock` Instead of `row_type` for PostgreSQL, Causing Incorrect Script Deletion During Block Rollback - (File: util/rich-indexer/src/indexer/remove.rs)

### Summary

In `util/rich-indexer/src/indexer/remove.rs`, the function `script_exists_in_output` contains a copy-paste error: the second `match` block at line 252 reads from `row_lock` (the result of the `lock_script_id` query) instead of `row_type` (the result of the `type_script_id` query). On PostgreSQL backends, this causes the function to always return the lock-script existence result, never the type-script existence result. Consequently, during `rollback_block`, scripts that are exclusively used as type scripts are incorrectly identified as absent and deleted from the database, corrupting the rich-indexer state.

### Finding Description

`script_exists_in_output` is designed to return `true` if a given `script_id` is referenced by any output row as either a lock script or a type script. It issues two separate SQL queries — `row_lock` (checking `lock_script_id`) and `row_type` (checking `type_script_id`) — and is supposed to return the logical OR of both results. [1](#0-0) 

The first half of the function correctly short-circuits on `row_lock`: [2](#0-1) 

But the second half, which is supposed to evaluate `row_type`, mistakenly re-reads `row_lock`:

```rust
// line 252 — BUG: row_lock used instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [3](#0-2) 

On **PostgreSQL**, `row_lock.try_get::<bool, _>(0)` always succeeds (the column is `BOOLEAN`), so the `Ok(r)` arm is always taken — returning the lock-script query result, never the type-script query result. On **SQLite**, `try_get::<bool, _>(0)` fails (the column is `BIGINT`), so the `Err` arm falls through to `row_type.get::<i64, _>(0) == 1`, which is correct.

The caller `rollback_block` uses this function to decide which scripts to delete: [4](#0-3) 

When `script_exists_in_output` incorrectly returns `false` for a type-only script, that script's row is added to `script_id_list_to_remove` and permanently deleted from the `script` table, even though live outputs still reference it.

### Impact Explanation

On PostgreSQL-backed deployments of the CKB rich-indexer, any block rollback (chain reorganization) that involves outputs whose scripts are used exclusively as type scripts (not as lock scripts) will silently delete those script records. After the rollback:

- Subsequent re-indexing of the same block will encounter foreign-key or consistency violations, or silently insert duplicate/orphaned rows.
- RPC queries against the rich-indexer (e.g., `get_cells`, `get_transactions` filtered by type script) will return incorrect or empty results for affected scripts.
- Applications that rely on the rich-indexer for determining cell ownership or script existence receive corrupted data, which can lead to application-level security failures (e.g., incorrectly concluding a cell is unspent or unlocked).

### Likelihood Explanation

Chain reorganizations are a normal, expected event on any live CKB network. Any unprivileged block relayer peer can trigger a reorg by propagating a valid competing chain of sufficient difficulty. Type scripts are widely used in CKB (UDT tokens, NFTs, DAO, etc.), so the affected code path is exercised in practice. The bug is silent — no error is logged and no panic occurs — so it can accumulate undetected across multiple reorgs. PostgreSQL is a supported and documented backend for the rich-indexer.

### Recommendation

Replace `row_lock` with `row_type` in the second `match` block:

```rust
// Correct fix:
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [3](#0-2) 

### Proof of Concept

1. Deploy a CKB node with the rich-indexer configured to use a **PostgreSQL** backend.
2. Submit a transaction whose output uses a script **only as a type script** (the lock script is a different, common script such as secp256k1-blake160).
3. Mine a block containing that transaction so the rich-indexer indexes it.
4. Trigger a chain reorganization that rolls back that block (e.g., by mining a longer competing chain from a peer).
5. Observe that `rollback_block` is called, which calls `script_exists_in_output(type_script_id, tx)`.
6. Because `row_lock.try_get::<bool, _>(0)` succeeds on PostgreSQL and returns `false` (the script is not a lock script), the function returns `Ok(false)`.
7. The type script's row is added to `script_id_list_to_remove` and deleted from the `script` table.
8. Query the rich-indexer for cells filtered by that type script — the result is empty, even if the cell still exists on-chain after the reorg resolves.

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
