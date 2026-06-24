The code at line 252 is confirmed. The bug exists exactly as described.

Audit Report

## Title
`script_exists_in_output` Uses `row_lock` Instead of `row_type` in Final Match on PostgreSQL, Causing Type Script Deletion During Reorg — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output` (line 204), after fetching both `row_lock` and `row_type` from two separate `SELECT EXISTS` queries, the final match block at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, `row_lock` holds a BOOLEAN that was already evaluated as `false` (the `true` case returned early at line 225–227), so the match always returns `Ok(false)`, and `row_type` is silently discarded. During `rollback_block`, any type script appearing in the rolled-back block's outputs is unconditionally added to the deletion list and removed from the `script` table, even if it is still referenced by live outputs in earlier blocks.

## Finding Description
The function `script_exists_in_output` (line 204) executes two queries:

1. **Lines 208–220**: Fetches `row_lock` — `SELECT EXISTS (... WHERE lock_script_id = $1)`.
2. **Lines 222–235**: Matches on `row_lock`; if the script exists as a lock script, returns `Ok(true)` early. Otherwise falls through.
3. **Lines 237–249**: Fetches `row_type` — `SELECT EXISTS (... WHERE type_script_id = $1)`.
4. **Line 252**: `match row_lock.try_get::<bool, _>(0)` — **copy-paste bug**: should be `row_type`.

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds (PG returns BOOLEAN). Since execution only reaches line 252 when `row_lock` was already `false`, the match arm `Ok(r)` at line 253 always returns `Ok(false)`. The `row_type` result is fetched but never read on PostgreSQL.

On SQLite, `row_lock.try_get::<bool, _>(0)` fails (SQLite returns BIGINT), so the `Err(_)` branch at line 255 correctly reads `row_type.get::<i64, _>(0) == 1`. The bug is PostgreSQL-only.

`rollback_block` (lines 28–39) calls `script_exists_in_output` for every type script in the rolled-back block's outputs and deletes all scripts for which it returned `false`. Because the function always returns `false` for the type-script check on PostgreSQL, every type script from the rolled-back block is deleted unconditionally, regardless of whether it is still referenced by outputs in earlier retained blocks.

## Impact Explanation
This is a **Medium** severity finding: **Suboptimal/incorrect implementation of CKB state storage mechanism** (2001–10000 points). The rich-indexer is the CKB state storage layer for RPC queries. The bug causes permanent, silent deletion of `script` table rows that are still referenced by live outputs. All subsequent `get_cells` / `get_transactions` RPC queries filtering by the deleted type script return zero results. Widely-shared type scripts (Nervos DAO, UDT type scripts) are highest-value targets: DAO deposits in earlier blocks become invisible to wallets, preventing DAO withdrawal construction. The corruption is persistent — once the script row is deleted, it is not recovered without re-indexing.

## Likelihood Explanation
Reorgs are a normal, unprivileged consensus event requiring no attacker capability beyond the chain producing a competing fork. Any PostgreSQL-backed rich-indexer node that experiences a reorg where the rolled-back block contains a type script also present in earlier blocks will silently corrupt its script index. The rich-indexer is a production component serving RPC queries.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

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
2. Index block A containing outputs with a type script (e.g., the Nervos DAO system script).
3. Index block B (tip) containing an output also using the same type script.
4. Trigger a reorg that rolls back block B (`rollback_block` is called).
5. Query `get_cells` with a filter on the type script hash.
6. **Expected**: Cells from block A are returned.
7. **Actual**: Zero results — the type script row was deleted from the `script` table because `script_exists_in_output` incorrectly returned `false` at line 252–253.

The bug is directly confirmed at [1](#0-0)  where `row_lock` is used instead of `row_type` in the final match, and the early-return logic at [2](#0-1)  confirms that reaching line 252 guarantees `row_lock` is already `false`. The deletion path is at [3](#0-2) .

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
