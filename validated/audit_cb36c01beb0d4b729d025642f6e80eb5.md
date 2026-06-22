The bug is real and confirmed. Let me analyze it precisely.

At line 252, the second `match` block re-reads `row_lock` instead of `row_type`: [1](#0-0) 

The function queries `row_type` (checking `type_script_id = $1`) at lines 237–249, but then at line 252 evaluates `row_lock.try_get::<bool, _>(0)` — the stale result from the first query — instead of `row_type.try_get::<bool, _>(0)`.

On **PostgreSQL**, `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN`), so the `Ok(r)` arm is taken and returns the lock-query result (`false`) — completely ignoring `row_type`. On **SQLite**, `try_get::<bool, _>` fails (SQLite returns `BIGINT`), so the `Err(_)` arm correctly falls through to `row_type.get::<i64, _>(0) == 1`. [2](#0-1) 

---

### Title
Copy-paste bug in `script_exists_in_output` causes premature script deletion on PostgreSQL during rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary
In `script_exists_in_output`, the second `match` at line 252 reads `row_lock` instead of `row_type`. On PostgreSQL this silently returns `false` for any script that is referenced **only** as a `type_script_id` (not as a `lock_script_id`) in surviving outputs, causing `rollback_block` to delete that script row even though live outputs still reference it.

### Finding Description
`rollback_block` calls `script_exists_in_output(script_id)` to decide whether a script row is still needed after the rolled-back block's outputs are removed. [3](#0-2) 

Inside `script_exists_in_output`:

1. `row_lock` is fetched: `SELECT EXISTS (SELECT 1 FROM output WHERE lock_script_id = $1)`.
2. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds. If `true`, returns early. If `false`, falls through.
3. `row_type` is fetched: `SELECT EXISTS (SELECT 1 FROM output WHERE type_script_id = $1)`.
4. **Bug**: line 252 evaluates `row_lock.try_get::<bool, _>(0)` again — not `row_type`. On PostgreSQL this always succeeds with the stale `false` value, so `Ok(false)` is returned regardless of whether `row_type` found a match. [4](#0-3) 

### Impact Explanation
Any script `S` that is used **only** as a `type_script_id` in surviving outputs will be incorrectly deleted from the `script` table during rollback. The surviving `output` rows retain their `type_script_id` foreign key pointing to the now-deleted script row. All subsequent `get_cells` / `get_transactions` RPC calls that join on `type_script_id` for `S` return zero results: [5](#0-4) 

Cells that exist on-chain become permanently invisible to any wallet or DApp querying via the rich-indexer, constituting loss of fund visibility.

### Likelihood Explanation
- Triggered by any chain reorganization — a routine event requiring no privileged access.
- Condition: the rolled-back block contains an output whose `type_script` is also used in a **surviving** block's output, but is **not** used as a `lock_script` anywhere. This is the common case for UDT/NFT type scripts.
- Affects only PostgreSQL deployments (SQLite accidentally takes the correct `Err` branch).
- No attacker capability beyond submitting valid blocks to the P2P network is required.

### Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// line 252 — fix: use row_type, not row_lock
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

### Proof of Concept
1. Start a PostgreSQL-backed rich-indexer node.
2. Append **block 0** containing output `O0` with a unique `type_script S` (and any `lock_script L`).
3. Append **block 1** containing output `O1` also using `S` as `type_script` (and a different `lock_script L2`).
4. Call `rollback_block` (simulating a 1-block reorg).
5. **Expected**: `script` table still contains `S`; `get_cells` for `S` returns `O0`.
6. **Actual (buggy)**: `script_exists_in_output(S_id)` returns `false` because `S` is not a `lock_script` anywhere → `S` is deleted → `get_cells` for `S` returns empty. [6](#0-5)

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
