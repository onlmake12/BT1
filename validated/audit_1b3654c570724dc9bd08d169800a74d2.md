The bug is confirmed in the code. Let me verify the exact behavior and impact.

The `script_exists_in_output` function at line 252 uses `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)` for the second match block: [1](#0-0) 

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (returns `Ok(false)` since the `true` case already returned early at line 225), so the `row_type` query result is completely ignored. On SQLite, `try_get::<bool, _>(0)` fails (SQLite returns BIGINT), so it falls to the `Err` branch which correctly reads `row_type`.

The rollback path that calls this function: [2](#0-1) 

The RPC queries that JOIN on `script.id` and would return empty results after the script is deleted: [3](#0-2) 

---

### Title
PostgreSQL-only copy-paste bug in `script_exists_in_output` causes incorrect script deletion during reorg, permanently corrupting indexer state — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary
A copy-paste bug at line 252 of `remove.rs` causes `script_exists_in_output` to re-read `row_lock` instead of `row_type` when checking whether a script is still referenced as a `type_script_id`. On PostgreSQL, this means any script used **only** as a `type_script_id` (not as a `lock_script_id`) in surviving outputs is incorrectly reported as unreferenced and deleted from the `script` table during a `rollback_block`. This permanently corrupts the indexer until a full re-sync.

### Finding Description
In `script_exists_in_output` (`remove.rs`, lines 204–257):

1. `row_lock` is fetched: `SELECT EXISTS (... WHERE lock_script_id = $1)` (line 208–220)
2. On PostgreSQL, if `row_lock` is `false`, execution continues past the early-return guard (line 223–235)
3. `row_type` is fetched: `SELECT EXISTS (... WHERE type_script_id = $1)` (line 237–249)
4. **Bug**: The second `match` at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (returning `Ok(false)`, since `true` already returned early), so `row_type` is never consulted. The function returns `false` even when the script is still referenced as a `type_script_id` in surviving outputs.

On SQLite, `try_get::<bool, _>(0)` fails (SQLite stores `EXISTS` as BIGINT), so the `Err(_)` branch correctly reads `row_type.get::<i64, _>(0)`. SQLite is unaffected.

### Impact Explanation
During `rollback_block`, after the rolled-back block's outputs are deleted from the `output` table, `script_exists_in_output` is called for each script referenced by those outputs. If a script `S` is used only as `type_script_id` in surviving outputs (from earlier blocks), the bug causes `S` to be added to `script_id_list_to_remove` and deleted from the `script` table. Subsequent `get_cells` and `get_transactions` RPC calls JOIN on `script.id`; with `S` deleted, those JOINs return no rows for cells whose `type_script_id` pointed to `S`. The indexer state is permanently corrupted until a full re-sync.

### Likelihood Explanation
Chain reorgs are a normal, unprivileged P2P event — any peer broadcasting a longer competing chain can trigger one. No special privileges, keys, or majority hashpower are required. The bug is deterministically triggered whenever a reorg rolls back a block that introduced a script `S` that is also used as `type_script_id` in an earlier surviving output, and the operator uses PostgreSQL (a supported and documented backend).

### Recommendation
Change line 252 from:
```rust
match row_lock.try_get::<bool, _>(0) {
```
to:
```rust
match row_type.try_get::<bool, _>(0) {
```

### Proof of Concept
1. Start a CKB rich-indexer with PostgreSQL backend.
2. Append `block0` containing an output with `type_script = S` (and any `lock_script`).
3. Append `block1` containing an output also using `S` as `type_script_id` only (not as `lock_script_id`).
4. Trigger rollback of `block1` (simulate a reorg).
5. Query the `script` table: `S` will be absent, even though `block0`'s output still references it.
6. Call `get_cells` with a filter for script `S` as type: returns zero results despite the cell existing.

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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L84-91)
```rust
        match search_key.script_type {
            IndexerScriptType::Lock => {
                query_builder.on("output.lock_script_id = query_script.id");
            }
            IndexerScriptType::Type => {
                query_builder.on("output.type_script_id = query_script.id");
            }
        }
```
