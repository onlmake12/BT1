The bug is real and concrete. The code at line 252 contains a copy-paste error: `row_lock` is used instead of `row_type` in the final match block of `script_exists_in_output`.

Here is the exact logic flow on PostgreSQL:

```
script_exists_in_output(script_id):
  1. Query row_lock  → EXISTS(lock_script_id = script_id)
  2. On PG: row_lock.try_get::<bool,_>(0) succeeds
     - if true  → return Ok(true)   ← early return, correct
     - if false → fall through
  3. Query row_type  → EXISTS(type_script_id = script_id)
  4. Line 252: match row_lock.try_get::<bool,_>(0)   ← BUG: should be row_type
     - On PG this succeeds and returns Ok(false)      ← always false here
     - row_type result is NEVER consulted on PG
``` [1](#0-0) 

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` at line 252 always succeeds (PG returns BOOLEAN), and since we only reach line 252 when `row_lock` was already `false` (the `true` case returned early at line 225), this match arm always evaluates to `Ok(false)`. The `row_type` result is silently discarded.

On SQLite, `row_lock.try_get::<bool, _>(0)` fails (SQLite returns BIGINT), so the `Err(_)` branch at line 255 correctly reads `row_type.get::<i64, _>(0)`. The bug is **PostgreSQL-only**.

---

### Title
`script_exists_in_output` Uses Wrong Row Variable on PostgreSQL, Causing Type Scripts Shared Across Blocks to Be Deleted During Reorg — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary
A copy-paste bug in `script_exists_in_output` causes the function to always return `false` for the type-script existence check on PostgreSQL. During `rollback_block`, any type script that appeared in the rolled-back block's outputs is unconditionally added to `script_id_list_to_remove` and deleted from the `script` table, even if that same script is still referenced by live outputs in earlier blocks.

### Finding Description
In `script_exists_in_output` (line 204), after fetching `row_lock` and `row_type` from two separate `SELECT EXISTS` queries, the final match at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. [2](#0-1) 

On PostgreSQL, `row_lock` holds a BOOLEAN result that was already evaluated as `false` (otherwise the function would have returned `true` at line 225–227). Re-reading it at line 252 always yields `Ok(false)`, so the function returns `false` regardless of what `row_type` contains. The `row_type` query result is fetched but never used on PostgreSQL.

`rollback_block` calls `script_exists_in_output` for every type script in the rolled-back block's outputs, then deletes all scripts for which it returned `false`: [3](#0-2) 

### Impact Explanation
Any type script that:
1. Appears in the rolled-back block's outputs, **and**
2. Is also referenced by outputs in earlier (retained) blocks

will be deleted from the `script` table. All subsequent `get_cells` / `get_transactions` RPC queries filtering by that type script will return zero results, because the script row no longer exists to join against. The Nervos DAO system script is a high-value example: DAO deposits in earlier blocks become invisible to wallets, preventing DAO withdrawal construction. The same applies to any widely-shared type script (UDT type scripts, etc.).

### Likelihood Explanation
Reorgs are a normal, unprivileged event in CKB consensus — no attacker capability is required beyond the chain producing a competing fork (which happens naturally). Any node running the rich-indexer against PostgreSQL and experiencing a reorg where the rolled-back block contains a type script also present in earlier blocks will silently corrupt its script index. The rich-indexer is a production component used to serve RPC queries.

### Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // was: row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

### Proof of Concept
1. Start a PostgreSQL-backed rich-indexer.
2. Index block A containing outputs with the DAO type script.
3. Index block B (tip) containing a DAO withdrawal output also using the DAO type script.
4. Trigger a reorg that rolls back block B (`rollback_block`).
5. Query `get_cells` with a filter on the DAO type script.
6. **Expected**: DAO deposit cells from block A are returned.
7. **Actual**: Zero results — the DAO type script row was deleted from the `script` table by `rollback_block` because `script_exists_in_output` incorrectly returned `false` for it.

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
