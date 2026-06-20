### Title
PostgreSQL-Only Copy-Paste Bug in `script_exists_in_output` Causes Permanent Script Table Corruption After Reorg — (`util/rich-indexer/src/indexer/remove.rs`)

---

### Summary

A copy-paste bug in `script_exists_in_output` causes the function to always return the result of the `lock_script_id` existence check on PostgreSQL, completely ignoring the `type_script_id` check. During `rollback_block`, any script used **only** as a `type_script` (not as a `lock_script`) in surviving outputs is incorrectly deleted from the `script` table. When the rolled-back block is re-appended (reorg), the script is re-inserted with a **new auto-incremented `script_id`**, leaving all prior outputs that referenced the old `script_id` with permanently dangling `type_script_id` foreign keys. Subsequent `get_cells` queries for that script silently omit those outputs.

---

### Finding Description

In `script_exists_in_output`, after the `lock_script_id` check fails, the code fetches `row_type` (the `type_script_id` existence result) but then mistakenly re-reads `row_lock` in the final match:

```rust
// Line 237-249: row_type is fetched correctly
let row_type = sqlx::query(
    r#"SELECT EXISTS (SELECT 1 FROM output WHERE type_script_id = $1)"#,
)
.bind(script_id)
.fetch_one(tx.as_mut())
.await?;

// Line 252: BUG — uses row_lock instead of row_type
match row_lock.try_get::<bool, _>(0) {   // <-- should be row_type
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [1](#0-0) 

On **PostgreSQL**, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL returns `BOOLEAN`), so the `Ok(r)` arm is always taken — returning the `lock_script_id` result a second time and never consulting `row_type`. On **SQLite**, `try_get::<bool, _>` fails, falling through to the `Err` arm which correctly reads `row_type`.

The caller in `rollback_block` uses this function to decide which scripts to delete after outputs are removed:

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
}
remove_batch_by_blobs("script", "id", &script_id_list_to_remove, tx).await?;
``` [2](#0-1) 

Because `script_exists_in_output` only checks `lock_script_id` on PostgreSQL, a script T that is referenced **only** as `type_script_id` in surviving outputs will be incorrectly added to `script_id_list_to_remove` and deleted.

On re-append, `bulk_insert_script_table` uses `ON CONFLICT (code_hash, hash_type, args) DO NOTHING`:

```rust
bulk_insert(
    "script",
    &["code_hash", "hash_type", "args"],
    &script_rows,
    Some(&["code_hash", "hash_type", "args"]),
    tx,
)
``` [3](#0-2) 

Since T was deleted, there is no conflict — T is re-inserted with a **new** auto-incremented `id`. All outputs from the pre-rollback block that stored the old `script_id` now have a dangling `type_script_id` foreign key.

---

### Impact Explanation

After a rollback+reappend cycle (reorg) on PostgreSQL:

- Outputs from block N-1 that used script T as `type_script` retain the old (now non-existent) `script_id`.
- `get_cells` queries that JOIN `output` on `script.id` will silently miss those outputs.
- The inconsistency is **permanent** — it cannot be corrected without a full re-index.
- Any application (DeFi, wallet, explorer) relying on the indexer for type-script cell queries will receive silently wrong results.

---

### Likelihood Explanation

Reorgs are a routine blockchain event, not an exceptional one. The conditions required are:

1. Any block containing an output with a `type_script` T.
2. A prior block also containing an output with the same `type_script` T (so T survives the rollback in the output table).
3. A reorg that rolls back the later block and re-appends it (or a different block at the same height).

This is a common pattern for any script used across multiple blocks (e.g., a standard token type script). No special attacker capability beyond submitting normal transactions is required; a natural reorg suffices.

---

### Recommendation

Fix the copy-paste error at line 252 of `remove.rs`: change `row_lock` to `row_type`:

```rust
// Before (buggy):
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}

// After (correct):
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [4](#0-3) 

---

### Proof of Concept

State test on PostgreSQL:

1. Append block N-1 with output O1: `lock_script = L1`, `type_script = T`. Record `T_old_id = SELECT id FROM script WHERE code_hash=... AND args=...`.
2. Append block N with output O2: `lock_script = L2`, `type_script = T`. (T already exists, `ON CONFLICT DO NOTHING`.)
3. Call `rollback()`. Internally, `script_exists_in_output(T_old_id)` checks only `lock_script_id = T_old_id` (false), ignores `type_script_id = T_old_id` (true on O1), and deletes T. Verify: `SELECT id FROM script WHERE id = T_old_id` → 0 rows.
4. Append block N again. T is re-inserted. `T_new_id = SELECT id FROM script WHERE code_hash=... AND args=...`. Assert `T_new_id != T_old_id`.
5. Query: `SELECT type_script_id FROM output WHERE id = O1_id`. Returns `T_old_id`. But `SELECT id FROM script WHERE id = T_old_id` → 0 rows. **Dangling reference confirmed.**
6. `get_cells(T)` → O1 is missing from results. **Inconsistency confirmed.**

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

**File:** util/rich-indexer/src/indexer/insert.rs (L347-354)
```rust
    bulk_insert(
        "script",
        &["code_hash", "hash_type", "args"],
        &script_rows,
        Some(&["code_hash", "hash_type", "args"]),
        tx,
    )
    .await
```
