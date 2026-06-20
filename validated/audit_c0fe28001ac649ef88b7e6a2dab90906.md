The bug is real and clearly visible in the source. Let me document the exact finding.

At line 252, after fetching `row_type` (the `type_script_id` EXISTS query), the final match statement reads from `row_lock` a second time instead of `row_type`: [1](#0-0) 

The PostgreSQL branch at line 252–253 does `row_lock.try_get::<bool, _>(0)` — re-reading the lock-script result — while the SQLite fallback at line 255 correctly reads `row_type.get::<i64, _>(0)`. So the bug only affects PostgreSQL-backed deployments.

---

### Title
Copy-paste bug in `script_exists_in_output` causes type-script rows to be deleted while live outputs still reference them during reorg rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary
`script_exists_in_output` checks two SQL EXISTS queries — one for `lock_script_id` and one for `type_script_id` — but on the PostgreSQL code path the final return reads `row_lock` a second time instead of `row_type`. When a script is used **only** as a type script (not as a lock script) in surviving outputs, the function incorrectly returns `false`, causing `rollback_block` to delete the script row even though live outputs still reference it.

### Finding Description
In `rollback_block` [2](#0-1) , after the rolled-back block's outputs are removed from the `output` table, `script_exists_in_output` is called for each lock/type script ID to decide whether the script row is still needed. [3](#0-2) 

The function fetches `row_lock` (EXISTS on `lock_script_id`) and, if that is false, fetches `row_type` (EXISTS on `type_script_id`). [4](#0-3) 

The final return, however, matches on `row_lock` again instead of `row_type`: [5](#0-4) 

```
// line 252 — BUG: should be row_type.try_get, not row_lock.try_get
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),  // SQLite path is correct
}
```

Result: on PostgreSQL, when `lock_script_id` check is false but `type_script_id` check would be true, the function returns `false`, and the script row is added to `script_id_list_to_remove` and deleted. [6](#0-5) 

### Impact Explanation
The `script` table row is deleted while one or more rows in the `output` table still hold a foreign reference to it via `type_script_id`. Any subsequent RPC query that JOINs `output` with `script` on `script.id` for those outputs will either return no results or produce an error, silently corrupting indexer query results for the affected outputs. This persists until the indexer is rebuilt from scratch.

### Likelihood Explanation
A reorg is a normal network event. The triggering condition — two outputs in different blocks sharing the same type script, where one block is rolled back and the other survives — is common in practice (e.g., any two outputs using the same token type script). No special attacker capability beyond submitting ordinary transactions is required; the reorg itself can be natural. The bug only fires on PostgreSQL-backed deployments (SQLite is unaffected due to the correct fallback at line 255).

### Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

### Proof of Concept
1. Start a CKB node with the rich-indexer backed by PostgreSQL.
2. Submit a block containing two outputs both using the same type script `S`; one output is in block B1 (tip), the other is in block B0 (already confirmed and surviving).
3. Trigger a reorg that rolls back B1 (e.g., by feeding a longer competing chain).
4. Query the `script` table: the row for `S` will be absent even though the output in B0 still references it.
5. Issue any RPC call (e.g., `get_cells`) that filters by type script `S`; it returns empty results despite the live output existing.

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

**File:** util/rich-indexer/src/indexer/remove.rs (L204-207)
```rust
async fn script_exists_in_output(
    script_id: i64,
    tx: &mut Transaction<'_, Any>,
) -> Result<bool, Error> {
```

**File:** util/rich-indexer/src/indexer/remove.rs (L208-256)
```rust
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
```
