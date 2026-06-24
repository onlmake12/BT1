The code at line 252 confirms the bug exactly as described. [1](#0-0) 

The function `script_exists_in_output` fetches `row_type` at lines 237–249 but then re-reads `row_lock` at line 252 instead of `row_type`. [2](#0-1) 

The early-return guard at lines 222–235 means execution only reaches line 252 when `row_lock` is already `false`, so on PostgreSQL (where `try_get::<bool, _>(0)` succeeds), the match always returns `Ok(false)`, discarding `row_type` entirely. [3](#0-2) 

On SQLite, `try_get::<bool, _>(0)` fails (SQLite returns BIGINT), so the `Err(_)` branch correctly reads `row_type.get::<i64, _>(0)`, masking the bug. [4](#0-3) 

`rollback_block` pushes every `false` result into `script_id_list_to_remove` and deletes them, so any type script from the rolled-back block is unconditionally deleted on PostgreSQL. [5](#0-4) 

---

Audit Report

## Title
`script_exists_in_output` Re-reads `row_lock` Instead of `row_type` on PostgreSQL, Corrupting Script Index During Reorg — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
At line 252 of `script_exists_in_output`, the final `match` reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this causes the function to always return `Ok(false)` for the type-script existence check, so `rollback_block` unconditionally deletes type scripts from the `script` table even when they are still referenced by retained outputs in earlier blocks, permanently corrupting the rich-indexer's script index.

## Finding Description
`script_exists_in_output` (line 204) issues two `SELECT EXISTS` queries: `row_lock` (lines 208–220) checks `lock_script_id = $1`; `row_type` (lines 237–249) checks `type_script_id = $1`. The first match block (lines 222–235) returns `Ok(true)` early if `row_lock` is true. Execution only reaches line 252 when `row_lock` is `false`. The match at line 252 is supposed to evaluate `row_type`, but instead re-reads `row_lock`:

```rust
// line 252 — BUG: row_lock should be row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `EXISTS` returns `BOOLEAN`, so `row_lock.try_get::<bool, _>(0)` succeeds and returns the already-known-false value of `row_lock`, always yielding `Ok(false)`. `row_type` is fetched but silently discarded. On SQLite, `EXISTS` returns `BIGINT`, so `try_get::<bool, _>` fails, the `Err(_)` branch fires, and `row_type.get::<i64, _>(0)` is correctly read — masking the bug on SQLite.

`rollback_block` (lines 28–39) calls `script_exists_in_output` for every type script in the rolled-back block's outputs and pushes any `false` result into `script_id_list_to_remove`, which is then deleted from the `script` table. Because `script_exists_in_output` always returns `false` for the type-script path on PostgreSQL, every type script from the rolled-back block is deleted regardless of whether it is still referenced by retained outputs.

## Impact Explanation
This is a confirmed correctness bug in the CKB rich-indexer's state storage mechanism. After any reorg on a PostgreSQL-backed rich-indexer node, type scripts shared between the rolled-back block and earlier retained blocks are permanently deleted from the `script` table. All subsequent RPC queries (`get_cells`, `get_transactions`) filtering by those type scripts return zero results. This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation
Reorgs are a normal, unprivileged consensus event requiring no attacker capability. Any PostgreSQL-backed rich-indexer node that experiences a reorg where the rolled-back block contains a type script also present in earlier blocks will silently corrupt its script index. This condition is met routinely in production (e.g., DAO deposits, UDT type scripts shared across many blocks). No special privileges or victim mistakes are required.

## Recommendation
Change line 252 from `row_lock` to `row_type`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // was: row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

## Proof of Concept
1. Start a PostgreSQL-backed rich-indexer node.
2. Index block A containing outputs with a type script (e.g., DAO type script with a known `script_id`).
3. Index block B (tip) containing an output also using the same type script.
4. Trigger a reorg that rolls back block B (`rollback_block` is called).
5. Query `get_cells` with a filter on that type script.
6. **Expected**: cells from block A are returned.
7. **Actual**: zero results — the type script row was deleted from `script` because `script_exists_in_output` returned `Ok(false)` at line 252–253 due to reading `row_lock` instead of `row_type`.

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
