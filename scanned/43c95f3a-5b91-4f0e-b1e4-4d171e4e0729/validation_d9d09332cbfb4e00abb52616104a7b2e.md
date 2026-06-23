The bug is real and concrete. Let me analyze the exact code.

The bug is confirmed and precisely located. Here is the full analysis:

---

### Title
Wrong Row Variable in `script_exists_in_output` Causes Type-Script Deletion on PostgreSQL During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary

`script_exists_in_output` fetches two rows — `row_lock` (lock script existence) and `row_type` (type script existence) — but the final match at line 252 evaluates `row_lock` a second time instead of `row_type`. On PostgreSQL this always returns `false` for the type-script branch, causing every type script that is not simultaneously a lock script to be deleted from the `script` table during any rollback, permanently corrupting the indexer database.

### Finding Description

The function `script_exists_in_output` is called during `rollback_block` to decide whether a script row is still referenced by surviving outputs before deleting it. [1](#0-0) 

The function first queries whether `script_id` appears as a `lock_script_id`: [2](#0-1) 

If that check is false it then queries whether `script_id` appears as a `type_script_id` into `row_type`: [3](#0-2) 

But the final decision match re-reads `row_lock` instead of `row_type`: [4](#0-3) 

```rust
// BUG: row_lock should be row_type on line 252
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

**PostgreSQL path (affected):** `EXISTS` returns a SQL `BOOLEAN`. `row_lock.try_get::<bool, _>(0)` succeeds (`Ok` branch). Because the early-return guard at lines 223-228 already confirmed `row_lock` is `false`, this match arm always returns `Ok(false)` — the `row_type` result is never consulted. Every type script that is not simultaneously a lock script is therefore reported as "not referenced" and pushed into `script_id_list_to_remove`.

**SQLite path (unaffected):** `EXISTS` returns a `BIGINT`. `row_lock.try_get::<bool, _>(0)` fails (`Err` branch), falling through to `row_type.get::<i64, _>(0) == 1`, which is correct. All existing tests use SQLite (`MEMORY_DB`), so the bug is invisible to the test suite. [5](#0-4) 

### Impact Explanation

After any rollback on a PostgreSQL-backed rich-indexer, type scripts from the rolled-back block are deleted from the `script` table even when they are still referenced by outputs in surviving blocks. All subsequent `get_cells` / `get_transactions` queries that filter by type script join on `type_script_id` and return empty results. The corruption is persistent; recovery requires a full re-index.

### Likelihood Explanation

Short reorgs (1–2 blocks) are a routine occurrence on any live CKB network due to propagation latency. No attacker action is required; the bug fires on every natural reorg on any PostgreSQL deployment. PostgreSQL is the recommended production backend for the rich-indexer.

### Recommendation

Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Add a rollback integration test against PostgreSQL that indexes two blocks sharing a type script, rolls back the second block, and asserts the `script` table row count is unchanged.

### Proof of Concept

1. Start a PostgreSQL-backed rich-indexer.
2. Index block A containing output O1 with `type_script T`.
3. Index block B containing output O2 with the same `type_script T` (same script hash → same `script.id`).
4. Trigger rollback of block B.
5. `script_exists_in_output(T.id)` is called. `row_lock` is `false` (T is not a lock script). `row_type` is `true` (O1 still references T). But `row_lock.try_get::<bool, _>(0)` returns `Ok(false)`, so the function returns `false`.
6. T is deleted from the `script` table.
7. Query `get_cells` with type script filter for T → empty result, even though O1 is a live cell. [6](#0-5)

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L29-37)
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

**File:** util/rich-indexer/src/tests/rollback.rs (L6-8)
```rust
async fn test_rollback_block_0() {
    let storage = connect_sqlite(MEMORY_DB).await;
    let indexer = AsyncRichIndexer::new(
```
