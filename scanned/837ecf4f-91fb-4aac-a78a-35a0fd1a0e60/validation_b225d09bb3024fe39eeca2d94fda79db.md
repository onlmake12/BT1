The bug is real and clearly visible in the code. Let me provide the full assessment.

---

### Title
`script_exists_in_output` checks `row_lock` twice instead of `row_type` on PostgreSQL, causing incorrect type-script deletion during reorg rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary

`script_exists_in_output` contains a copy-paste bug at line 252: after fetching `row_type` (the `type_script_id` EXISTS query), the final `match` branch reads `row_lock` again instead of `row_type`. On PostgreSQL this silently returns the lock-script result for the type-script check, causing type scripts that are still referenced by surviving outputs to be incorrectly deleted during `rollback_block`.

### Finding Description

In `script_exists_in_output`: [1](#0-0) 

The function fetches `row_type` (line 237–249) but then at line 252 evaluates `row_lock.try_get::<bool, _>(0)` again instead of `row_type.try_get::<bool, _>(0)`. The `row_type` variable is only used in the `Err(_)` branch (line 255), which is the SQLite path. On PostgreSQL, `try_get::<bool, _>(0)` succeeds, so the `Ok(r)` arm at line 253 returns the stale `row_lock` boolean — which is already known to be `false` (otherwise the early-return at lines 224–227 would have fired). The function therefore always returns `Ok(false)` for the type-script check on PostgreSQL, regardless of whether the script is still referenced.

This feeds into `rollback_block`: [2](#0-1) 

Every `type_script_id` from the rolled-back block's outputs is unconditionally pushed into `script_id_list_to_remove` and then deleted, even when surviving outputs in other blocks still hold a foreign-key reference to that `script.id`.

### Impact Explanation

**Scenario A — PostgreSQL with FK constraints enforced:**
`remove_batch_by_blobs("script", ...)` issues a `DELETE FROM script WHERE id IN (...)`. PostgreSQL raises a FK violation because surviving `output` rows still reference those `script.id` values. The error propagates as `Error::DB(...)` out of `rollback_block` → `AsyncRichIndexer::rollback` → `RichIndexer::rollback`. The DB transaction is aborted, the blockchain rollback never completes, and the indexer stalls permanently until manually restarted. [3](#0-2) 

**Scenario B — PostgreSQL without FK constraints / SQLite:**
The delete succeeds, but surviving outputs now have dangling `type_script_id` references. Subsequent `append` calls re-insert the script via `bulk_insert_script_table` (which uses `ON CONFLICT DO NOTHING`), assigning it a new `id`. The surviving outputs still hold the old, now-orphaned `id`, causing all JOIN-based queries on those outputs to return NULL type-script data — silent, permanent data corruption. [4](#0-3) 

### Likelihood Explanation

- Reorgs are a normal, frequent occurrence on CKB mainnet; no attacker is required.
- Type scripts (UDTs, NFTs, DAO) are ubiquitous and routinely shared across many blocks.
- PostgreSQL is a first-class supported backend for the rich-indexer.
- The condition "a type script from the rolled-back block is also used in a surviving block" is trivially satisfied by any token transfer or DAO operation spanning multiple blocks.

### Recommendation

Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// line 252 — fix: use row_type, not row_lock
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [5](#0-4) 

### Proof of Concept

1. Start rich-indexer with a PostgreSQL backend.
2. Append block N containing tx with output O1 whose `type_script` is script S (also present in a surviving block N-1 output O0).
3. Trigger a reorg that rolls back block N.
4. Observe: `script_exists_in_output(S.id)` returns `false` (bug), S is deleted.
5. On PostgreSQL with FK: `rollback_block` returns `Error::DB("foreign key violation")`, indexer stalls.
6. On PostgreSQL without FK: O0's `type_script_id` now points to a deleted row; subsequent queries on O0 return NULL type script.
7. Assert `SELECT COUNT(*) FROM script WHERE id = <S.id>` equals 1 after rollback — it equals 0, proving incorrect deletion.

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

**File:** util/rich-indexer/src/indexer/remove.rs (L54-83)
```rust
async fn remove_batch_by_blobs(
    table_name: &str,
    column_name: &str,
    ids: &[i64],
    tx: &mut Transaction<'_, Any>,
) -> Result<(), Error> {
    if ids.is_empty() {
        return Ok(());
    }

    // build query str
    let mut query_builder = SqlBuilder::delete_from(table_name);
    let sql = query_builder
        .and_where_in(column_name, &sqlx_param_placeholders(1..ids.len())?)
        .sql()
        .map_err(|err| Error::DB(err.to_string()))?;

    // bind
    let mut query: sqlx::query::Query<'_, Any, sqlx::any::AnyArguments<'_>> = sqlx::query(&sql);
    for hash in ids {
        query = query.bind(hash);
    }

    // execute
    query
        .execute(tx.as_mut())
        .await
        .map_err(|err| Error::DB(err.to_string()))?;

    Ok(())
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

**File:** util/rich-indexer/src/indexer/insert.rs (L338-355)
```rust
pub(crate) async fn bulk_insert_script_table(
    script_set: HashSet<(Vec<u8>, i16, Vec<u8>)>,
    tx: &mut Transaction<'_, Any>,
) -> Result<(), Error> {
    // let script_rows = script_set.iter().collect::<Vec<_>>();
    let script_rows = script_set
        .into_iter()
        .map(|(code_hash, hash_type, args)| vec![code_hash.into(), hash_type.into(), args.into()])
        .collect::<Vec<_>>();
    bulk_insert(
        "script",
        &["code_hash", "hash_type", "args"],
        &script_rows,
        Some(&["code_hash", "hash_type", "args"]),
        tx,
    )
    .await
}
```
