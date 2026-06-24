The code is confirmed. Line 252 clearly shows `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`.

Audit Report

## Title
`script_exists_in_output` Uses Wrong Row Variable on PostgreSQL, Causing Type Scripts Shared Across Blocks to Be Deleted During Reorg — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
A copy-paste bug at line 252 of `script_exists_in_output` reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this causes the function to always return `false` for the type-script existence check, so any type script appearing in a rolled-back block's outputs is unconditionally deleted from the `script` table during `rollback_block`, even if that script is still referenced by live outputs in earlier blocks. Subsequent RPC queries filtering by those type scripts return empty results.

## Finding Description
`script_exists_in_output` (line 204) performs two `SELECT EXISTS` queries: `row_lock` checks `lock_script_id = $1` and `row_type` checks `type_script_id = $1`. After the lock check, if the script exists as a lock script, the function returns `Ok(true)` early at lines 225–227. If not, execution falls through to fetch `row_type` at lines 237–249. The final match at line 252 is then supposed to evaluate `row_type`, but instead re-reads `row_lock`:

```rust
// line 252 — BUG: row_lock should be row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `EXISTS` returns `BOOLEAN`, so `row_lock.try_get::<bool, _>(0)` succeeds. Since execution only reaches line 252 when `row_lock` was already `false` (the `true` case exited at line 225–227), this match arm always evaluates to `Ok(false)`. The `row_type` result is fetched but silently discarded on every PostgreSQL call. On SQLite, `EXISTS` returns `BIGINT`, so `row_lock.try_get::<bool, _>(0)` fails, the `Err(_)` branch fires, and `row_type.get::<i64, _>(0)` is correctly read — masking the bug on SQLite.

`rollback_block` (lines 28–39) calls `script_exists_in_output` for every type script in the rolled-back block's outputs and pushes any `false` result into `script_id_list_to_remove`, which is then deleted from the `script` table. Because `script_exists_in_output` always returns `false` for the type-script path on PostgreSQL, every type script from the rolled-back block is deleted regardless of whether it is still referenced by retained outputs.

## Impact Explanation
This is a correctness bug in the CKB rich-indexer's state storage mechanism. After any reorg on a PostgreSQL-backed rich-indexer node, type scripts shared between the rolled-back block and earlier retained blocks are permanently deleted from the `script` table. All RPC queries (`get_cells`, `get_transactions`) that filter by those type scripts subsequently return zero results from that node. This constitutes a suboptimal (incorrect) implementation of the CKB state storage mechanism, matching **Medium (2001–10000 points)**.

## Likelihood Explanation
Reorgs are a normal, unprivileged consensus event requiring no attacker capability. Any PostgreSQL-backed rich-indexer node that experiences a reorg where the rolled-back block contains a type script also present in earlier blocks will silently corrupt its script index. The condition is met routinely in production (e.g., DAO deposits, UDT type scripts shared across many blocks).

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
2. Index block A containing outputs with a type script (e.g., DAO type script).
3. Index block B (tip) containing an output also using the same type script.
4. Trigger a reorg that rolls back block B (`rollback_block` is called).
5. Query `get_cells` with a filter on that type script.
6. **Expected**: cells from block A are returned.
7. **Actual**: zero results — the type script row was deleted from `script` because `script_exists_in_output` returned `false` at line 252–253 due to reading `row_lock` instead of `row_type`. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** util/rich-indexer/src/indexer/remove.rs (L251-256)
```rust
    // pg type is BOOLEAN
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```
