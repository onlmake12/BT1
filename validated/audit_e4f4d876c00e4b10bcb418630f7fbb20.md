The bug is confirmed. Let me analyze the exact behavior.

In `script_exists_in_output`, at line 252, the second `match` block reads from `row_lock` instead of `row_type`: [1](#0-0) 

The function queries `row_type` (type_script_id existence) correctly, but then at line 252 evaluates `row_lock.try_get::<bool,_>(0)` — the already-consumed lock result — instead of `row_type.try_get::<bool,_>(0)`.

**PostgreSQL vs SQLite behavior diverges here:**
- **PostgreSQL**: `row_lock.try_get::<bool,_>(0)` succeeds (`Ok(r)`), returning the lock-script existence boolean instead of the type-script existence boolean. The bug is active.
- **SQLite**: `row_lock.try_get::<bool,_>(0)` fails (`Err(_)`), falling through to `row_type.get::<i64,_>(0) == 1` at line 255, which is correct. The bug is **not** active on SQLite. [2](#0-1) 

---

### Title
Copy-paste bug in `script_exists_in_output` causes type scripts still referenced by live outputs to be incorrectly deleted during rollback on PostgreSQL — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary
A copy-paste error at line 252 of `remove.rs` causes `script_exists_in_output` to re-read `row_lock` (the lock_script_id existence result) instead of `row_type` (the type_script_id existence result) when running against PostgreSQL. During a block rollback, this causes scripts that are exclusively used as `type_script_id` in remaining live outputs to be incorrectly deleted from the `script` table, permanently corrupting the indexer's script-to-cell mapping.

### Finding Description
`script_exists_in_output` is called during `rollback_block` to determine whether a script ID is still referenced by any remaining output before deleting it from the `script` table. [3](#0-2) 

The function performs two SQL `EXISTS` queries: one checking `lock_script_id = $1` (stored in `row_lock`) and one checking `type_script_id = $1` (stored in `row_type`). The first match block correctly short-circuits if the lock check is true. The second match block at line 252 is supposed to evaluate `row_type`, but instead evaluates `row_lock` again:

```rust
// BUG: should be row_type.try_get::<bool, _>(0)
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),                              // returns lock result, not type result
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1) // SQLite fallback is correct
}
```

On PostgreSQL, `try_get::<bool,_>(0)` on `row_lock` always succeeds, so `r` is the lock-script existence boolean — not the type-script existence boolean. The SQLite fallback path (`Err(_)`) is never reached on PostgreSQL, so SQLite is unaffected.

**Concrete failure scenario (PostgreSQL only):**
1. Block N is appended containing a cell with a unique type script `T` (not used as a lock script anywhere).
2. Block N+1 is appended spending that cell.
3. A reorg causes rollback of block N+1. The outputs of block N+1 are removed from the `output` table.
4. `rollback_block` iterates the outputs of block N+1 and calls `script_exists_in_output(type_script_id_of_T, tx)`.
5. `row_lock` query: `T` is not a lock script → returns `false`.
6. First match: `Ok(false)` → does not short-circuit.
7. `row_type` query: `T` is still a type script on block N's live cell → returns `true`.
8. Second match (line 252): reads `row_lock` again → `Ok(false)` → returns `false`.
9. `T` is pushed to `script_id_list_to_remove` and deleted from the `script` table.
10. Block N's live cell still references `T` via `type_script_id`, but the script row no longer exists.

### Impact Explanation
After the incorrect deletion, any RPC query that joins `output` to `script` on `type_script_id` (e.g., `get_cells` with `script_type: Type`) will find no matching script row and silently return empty results for valid live cells. [4](#0-3) 

The corruption is permanent — the script row is hard-deleted and not recoverable without a full re-index. Applications (DeFi, token protocols, NFT platforms) that rely on type-script cell queries will silently receive incorrect data.

### Likelihood Explanation
- Chain reorgs are a normal, unprivileged event in CKB. Any peer can relay a valid competing chain tip that triggers a rollback.
- The bug fires on any rollback involving a cell whose type script is not also used as a lock script in any other remaining output — a common pattern for unique type scripts (e.g., TYPE_ID cells, UDT cells).
- Only PostgreSQL deployments are affected; SQLite deployments are not.
- No special privileges, keys, or majority hashpower are required.

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
1. Start a CKB node with the rich-indexer backed by PostgreSQL.
2. Append block N containing a transaction with an output cell using a unique type script `T` (not used as any lock script).
3. Append block N+1 spending that cell.
4. Trigger rollback of block N+1 (e.g., via a competing fork).
5. Query the `script` table: `SELECT * FROM script WHERE id = <T_id>` → row is absent.
6. Call `get_cells` RPC with `script_type: Type` matching `T` → returns empty, despite the live cell from block N still existing in the `output` table with `type_script_id = <T_id>`.

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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L84-107)
```rust
        match search_key.script_type {
            IndexerScriptType::Lock => {
                query_builder.on("output.lock_script_id = query_script.id");
            }
            IndexerScriptType::Type => {
                query_builder.on("output.type_script_id = query_script.id");
            }
        }
        query_builder
            .join("ckb_transaction")
            .on("output.tx_id = ckb_transaction.id")
            .join("block")
            .on("ckb_transaction.block_id = block.id");
        match search_key.script_type {
            IndexerScriptType::Lock => query_builder
                .left()
                .join(name!("script";"type_script"))
                .on("output.type_script_id = type_script.id"),
            IndexerScriptType::Type => query_builder
                .left()
                .join(name!("script";"lock_script"))
                .on("output.lock_script_id = lock_script.id"),
        }
        .and_where("output.is_spent = 0"); // live cells
```
