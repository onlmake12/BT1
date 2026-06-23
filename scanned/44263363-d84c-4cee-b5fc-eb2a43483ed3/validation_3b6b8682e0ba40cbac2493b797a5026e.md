The bug at line 252 is real and unambiguous. Let me confirm the exact behavior before rendering a verdict.

The `script_exists_in_output` function in `util/rich-indexer/src/indexer/remove.rs`:

- Lines 208–220: queries `row_lock` — does `script_id` appear as a `lock_script_id` in any surviving output?
- Lines 223–235: if `row_lock` is true (PostgreSQL) or `== 1` (SQLite), returns `Ok(true)` early.
- Lines 237–249: queries `row_type` — does `script_id` appear as a `type_script_id` in any surviving output?
- **Line 252**: the final match uses `row_lock.try_get::<bool, _>(0)` — **not** `row_type`.

On **PostgreSQL**: `row_lock.try_get::<bool, _>(0)` succeeds and returns `Ok(r)` where `r` is the stale lock-script EXISTS result. Since we only reach line 252 when `r` was already `false` (the early-return at line 225 was not taken), this always returns `Ok(false)` — the type-script query result is silently discarded.

On **SQLite**: `row_lock.try_get::<bool, _>(0)` fails (SQLite returns BIGINT), so the `Err(_)` arm at line 255 correctly reads `row_type.get::<i64, _>(0) == 1`. SQLite is unaffected.

---

### Title
Wrong row variable in `script_exists_in_output` causes premature script deletion on PostgreSQL during rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary
In `script_exists_in_output`, the final `match` at line 252 re-reads `row_lock` (the lock-script EXISTS result) instead of `row_type` (the type-script EXISTS result). On PostgreSQL this silently returns `false` for any script that is referenced only as a type script in surviving outputs, causing `rollback_block` to delete that script from the `script` table even though live outputs still reference it.

### Finding Description
`rollback_block` (lines 7–52) removes the tip block's outputs from the `output` table, then calls `script_exists_in_output` for each script ID to decide whether the script row is still needed. The function is supposed to return `true` if the script ID appears in any remaining output row as either `lock_script_id` or `type_script_id`.

The PostgreSQL code path at line 252 reads:

```rust
// line 252 — BUG: should be row_type, not row_lock
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Because `row_lock` holds the result of the lock-script EXISTS query, and we only reach line 252 when that result was already `false`, this branch always returns `Ok(false)` on PostgreSQL regardless of what `row_type` contains. [1](#0-0) 

The correct variable at line 252 should be `row_type`, matching the SQLite fallback at line 255 which already uses `row_type` correctly. [2](#0-1) 

### Impact Explanation
After a rollback on a PostgreSQL-backed rich-indexer:

1. Any script that was used **only as a type script** (never as a lock script) in surviving outputs is incorrectly added to `script_id_list_to_remove`.
2. `remove_batch_by_blobs("script", ...)` deletes those script rows.
3. Surviving `output` rows still hold the now-dangling `type_script_id` foreign key.
4. Subsequent RPC calls (`get_cells`, `get_transactions` filtered by type script) return empty or incorrect results for those cells.
5. The indexer database is silently corrupted; the only recovery is a full re-sync. [3](#0-2) 

### Likelihood Explanation
- Triggered by any chain reorganization (reorg) on a node running the rich-indexer with a PostgreSQL backend — a normal, unprivileged network event.
- The scenario requires only two blocks: block A with an output whose type script S is not also a lock script, and block B (later rolled back) that also references S as a type script. After rollback, S is deleted while block A's output still references it.
- SQLite deployments are unaffected; only PostgreSQL deployments exhibit the bug.

### Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// correct fix
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

### Proof of Concept
1. Start CKB rich-indexer with PostgreSQL backend.
2. Append block A containing a transaction with output `{lock: L1, type: S}`.
3. Append block B containing a transaction with output `{lock: L2, type: S}` (same type script S, different lock).
4. Trigger rollback of block B.
5. Query `SELECT * FROM script WHERE id = <S_id>` — the row is absent even though block A's output still references it.
6. RPC `get_cells` filtered by type script S returns 0 results instead of 1. [4](#0-3)

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
