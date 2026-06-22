The bug is real and confirmed. At line 252, `row_lock.try_get::<bool, _>(0)` is used instead of `row_type.try_get::<bool, _>(0)`. Here is the precise analysis:

---

### Title
Incorrect Variable Reference in `script_exists_in_output` Causes Type Script Premature Deletion on PostgreSQL During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary

A copy-paste bug at line 252 of `remove.rs` causes `script_exists_in_output` to read from `row_lock` (the lock-script existence query result) instead of `row_type` (the type-script existence query result) when running against PostgreSQL. This makes the function always return `false` for the type-script check on PostgreSQL, causing type scripts that are still referenced by surviving outputs to be incorrectly deleted from the `script` table during `rollback_block`.

### Finding Description

`script_exists_in_output` performs two SQL `EXISTS` queries:

1. `row_lock` — checks `WHERE lock_script_id = $1` [1](#0-0) 
2. `row_type` — checks `WHERE type_script_id = $1` [2](#0-1) 

The first block (lines 223–235) correctly short-circuits and returns `Ok(true)` if `row_lock` is true. If execution reaches line 252, `row_lock` is **guaranteed to be `false`**. The second match block at line 252 then reads `row_lock` again instead of `row_type`:

```rust
// BUG: should be row_type.try_get::<bool, _>(0)
match row_lock.try_get::<bool, _>(0) {   // line 252
    Ok(r) => Ok(r),                       // always Ok(false) on PostgreSQL
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),  // SQLite path — correct
}
``` [3](#0-2) 

On **PostgreSQL**, `try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN` for `EXISTS`), so the `Ok(r)` arm is taken and returns `Ok(false)` unconditionally — ignoring `row_type` entirely.

On **SQLite**, `try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err(_)` arm is taken and correctly reads `row_type`. SQLite is unaffected.

The caller in `rollback_block` uses this result to decide whether to delete a script:

```rust
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
``` [4](#0-3) 

Because `script_exists_in_output` always returns `false` for the type-script check on PostgreSQL, every type script from the rolled-back block is unconditionally added to `script_id_list_to_remove` and deleted, even when other surviving outputs still reference it via `type_script_id`.

### Impact Explanation

After a reorg on a PostgreSQL-backed rich-indexer node:
- The `script` table loses rows that are still referenced by the `output` table via `type_script_id`.
- Subsequent RPC queries (e.g., `get_cells`, `get_transactions` filtered by type script) that join `output` and `script` will return incomplete or missing results.
- The indexer database is left in an inconsistent state that persists until a full re-sync.

### Likelihood Explanation

- Chain reorgs are a normal, unprivileged blockchain event — any competing chain tip triggers `rollback_block`.
- PostgreSQL is a supported and documented backend for the rich-indexer.
- The bug is deterministic: every reorg on PostgreSQL that involves outputs with type scripts will trigger it.
- No special attacker capability is needed; a natural reorg suffices.

### Recommendation

Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// Fix:
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [5](#0-4) 

### Proof of Concept

1. Run a PostgreSQL-backed rich-indexer node.
2. Submit a block containing a transaction with two outputs sharing the same type script (but different lock scripts).
3. Trigger a reorg that rolls back that block (`rollback_block` is called).
4. Query the `script` table: the type script row will be absent.
5. Query the `output` table: surviving outputs (from other blocks) still reference the deleted `type_script_id`.
6. Issue an RPC call filtered by that type script — results are empty/incorrect despite the outputs existing.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L33-37)
```rust
        if let Some(type_script_id) = type_script_id
            && !script_exists_in_output(type_script_id, tx).await?
        {
            script_id_list_to_remove.push(type_script_id);
        }
```

**File:** util/rich-indexer/src/indexer/remove.rs (L208-220)
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
