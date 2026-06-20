The bug at line 252 is real and unambiguous. Let me confirm the exact code path before writing the finding.

The `script_exists_in_output` function at lines 204–257 of `util/rich-indexer/src/indexer/remove.rs`:

- Lines 208–220: Queries `row_lock` — does `script_id` appear as `lock_script_id` in any surviving output?
- Lines 223–235: If PostgreSQL returns `true`, returns early. If `false`, falls through.
- Lines 237–249: Queries `row_type` — does `script_id` appear as `type_script_id` in any surviving output? Result stored in `row_type`.
- **Line 252**: `match row_lock.try_get::<bool, _>(0)` — reads `row_lock` again instead of `row_type`. This is a copy-paste bug.

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds (returns `false`), so `row_type` is never consulted. On SQLite, `try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err` branch at line 255 correctly reads `row_type`. The bug is **PostgreSQL-only**.

---

### Title
Wrong row variable in `script_exists_in_output` causes type-script-only script rows to be incorrectly deleted during rollback on PostgreSQL — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary
A copy-paste error at line 252 of `script_exists_in_output` reads `row_lock` (the lock-script EXISTS result) instead of `row_type` (the type-script EXISTS result) when deciding whether a script is still referenced. On a PostgreSQL backend, this causes any script that is referenced **only** via `type_script_id` (not `lock_script_id`) in surviving outputs to be incorrectly deleted from the `script` table during a block rollback, leaving dangling foreign-key references in the `output` table and corrupting the indexer database.

### Finding Description
In `rollback_block`, after deleting the rolled-back block's output rows, the code iterates over each `(lock_script_id, type_script_id)` pair and calls `script_exists_in_output` to decide whether the script is still needed: [1](#0-0) 

Inside `script_exists_in_output`, two SQL `EXISTS` queries are issued — one for `lock_script_id` and one for `type_script_id`: [2](#0-1) [3](#0-2) 

The final match at line 252 is supposed to read `row_type` but instead reads `row_lock` again: [4](#0-3) 

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds and returns `false` (the script is not a `lock_script_id` of any surviving output). The function therefore returns `Ok(false)`, signalling that the script can be safely deleted — even though `row_type` would have returned `true`. The script is then added to `script_id_list_to_remove` and deleted from the `script` table, while surviving `output` rows still hold a `type_script_id` pointing to the now-deleted script row.

The `output` table schema has no `ON DELETE CASCADE` or foreign-key constraint enforcing referential integrity: [5](#0-4) 

So the deletion silently succeeds, leaving orphaned `type_script_id` values.

### Impact Explanation
After the corrupt rollback, any RPC call that queries by the deleted script (e.g., `get_cells`, `get_transactions`) will return empty or wrong results because the script row no longer exists. Repeated reorgs on the same node compound the corruption. The rich-indexer becomes permanently unreliable for all RPC consumers until the database is manually rebuilt from scratch.

### Likelihood Explanation
The trigger is a standard chain reorg on a PostgreSQL-backed rich-indexer node. Reorgs are a normal part of blockchain operation and do not require attacker-controlled hashpower — a natural competing tip or a single-block reorg by any miner is sufficient. The only precondition is that the rolled-back block contains an output whose `type_script_id` is shared with a surviving output in an earlier block, and that script is not also used as a `lock_script_id` anywhere. This is a common pattern (e.g., UDT type scripts, DAO type scripts). The bug is deterministic and reproducible.

### Recommendation
At line 252, replace `row_lock` with `row_type`:

```rust
// BEFORE (buggy):
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}

// AFTER (correct):
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

### Proof of Concept
1. Start a rich-indexer node with a PostgreSQL backend.
2. Append `block0` containing a transaction with an output whose `type_script_id = S` and `lock_script_id = L` (where `S ≠ L`).
3. Append `block1` containing a transaction with an output also using `type_script_id = S` (same script, different lock).
4. Trigger rollback of `block1` (simulate a reorg).
5. Query the `script` table for script `S`.

**Expected**: Script `S` still exists (block0's output still references it via `type_script_id`).
**Actual (buggy PostgreSQL path)**: Script `S` has been deleted. The `output` row from block0 now has a dangling `type_script_id` reference. All subsequent `get_cells`/`get_transactions` RPC calls for script `S` return empty results.

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

**File:** util/rich-indexer/src/indexer/remove.rs (L208-235)
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
```

**File:** util/rich-indexer/src/indexer/remove.rs (L237-249)
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
```

**File:** util/rich-indexer/src/indexer/remove.rs (L251-256)
```rust
    // pg type is BOOLEAN
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```

**File:** util/rich-indexer/resources/create_postgres_table.sql (L54-77)
```sql
CREATE TABLE IF NOT EXISTS output(
    id BIGSERIAL PRIMARY KEY,
    tx_id BIGINT NOT NULL,
    output_index INTEGER NOT NULL,
    capacity BIGINT NOT NULL,
    lock_script_id BIGINT,
    type_script_id BIGINT,
    data BYTEA
);

CREATE TABLE IF NOT EXISTS input(
    output_id BIGINT PRIMARY KEY,
    since BYTEA NOT NULL,
    consumed_tx_id BIGINT NOT NULL,
    input_index INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS script(
    id BIGSERIAL PRIMARY KEY,
    code_hash BYTEA NOT NULL,
    hash_type SMALLINT NOT NULL,
    args BYTEA,
    UNIQUE(code_hash, hash_type, args)
);
```
