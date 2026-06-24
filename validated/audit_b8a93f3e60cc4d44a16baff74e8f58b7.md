The code at line 252 is confirmed exactly as described. The bug is real.

Audit Report

## Title
Wrong row variable in `script_exists_in_output` silently discards type-script EXISTS result on PostgreSQL during rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the final `match` at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this always returns `Ok(false)` for the type-script check because `row_lock` held `false` when the early-return at line 225 was not taken. As a result, `rollback_block` incorrectly deletes script rows that are still referenced as type scripts in surviving outputs, silently corrupting the rich-indexer's PostgreSQL database.

## Finding Description
`rollback_block` (lines 7–52) removes the tip block's outputs, then calls `script_exists_in_output` for each `lock_script_id` and `type_script_id` to decide whether the script row is still needed. The function queries `row_lock` (lock-script EXISTS) and, if false, queries `row_type` (type-script EXISTS). The final match at line 252 is:

```rust
// line 252 — BUG: row_lock used instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns BOOLEAN) and returns `Ok(r)` where `r` is the stale lock-script result. Since we only reach line 252 when `r` was already `false` (the early-return at line 225–227 was not taken), this branch always returns `Ok(false)` regardless of `row_type`'s value. The type-script query result is silently discarded.

On SQLite, `row_lock.try_get::<bool, _>(0)` fails (SQLite returns BIGINT), so the `Err(_)` arm at line 255 correctly reads `row_type.get::<i64, _>(0) == 1`. SQLite is unaffected.

The consequence: any `script_id` that appears only as a `type_script_id` in surviving outputs is incorrectly added to `script_id_list_to_remove` (lines 28–38) and deleted by `remove_batch_by_blobs("script", ...)` at line 39, leaving dangling `type_script_id` foreign keys in the `output` table.

## Impact Explanation
This is a confirmed incorrect implementation of the CKB rich-indexer state storage mechanism. After a rollback on a PostgreSQL-backed rich-indexer, the `script` table is silently corrupted: script rows still referenced by surviving outputs are deleted. Subsequent RPC calls (`get_cells`, `get_transactions`) filtered by those type scripts return empty or incorrect results. Recovery requires a full re-sync. This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation
Triggered by any chain reorganization (reorg) on a node running the rich-indexer with a PostgreSQL backend — a normal, unprivileged network event requiring no attacker capability. The minimal scenario is two blocks: block A with an output whose type script S is not also a lock script, and block B (later rolled back) that also references S as a type script. After rollback, S is deleted while block A's output still references it. SQLite deployments are unaffected.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// correct fix
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

## Proof of Concept
1. Start CKB rich-indexer with PostgreSQL backend.
2. Append block A containing a transaction with output `{lock: L1, type: S}`.
3. Append block B containing a transaction with output `{lock: L2, type: S}` (same type script S, different lock).
4. Trigger rollback of block B.
5. Query `SELECT * FROM script WHERE id = <S_id>` — the row is absent even though block A's output still references it.
6. RPC `get_cells` filtered by type script S returns 0 results instead of 1. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** util/rich-indexer/src/indexer/remove.rs (L222-235)
```rust
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

**File:** util/rich-indexer/src/indexer/remove.rs (L251-256)
```rust
    // pg type is BOOLEAN
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```
