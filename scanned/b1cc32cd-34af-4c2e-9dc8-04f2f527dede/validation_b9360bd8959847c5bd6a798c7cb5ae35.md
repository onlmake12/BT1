The code at line 252 is confirmed exactly as described. Let me verify the impact classification against the allowed scope. [1](#0-0) 

The bug is real: after fetching `row_type` (the EXISTS result for `type_script_id`), the final `match` at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL returns BOOLEAN for EXISTS), so `Ok(r)` is returned using the **lock** query result, silently discarding `row_type`. On SQLite, `try_get::<bool, _>(0)` fails (BIGINT), so the `Err(_)` branch correctly reads `row_type` — SQLite is unaffected. [2](#0-1) 

The rollback path at lines 33–37 calls `script_exists_in_output` for each type script and deletes it if the function returns false — which it incorrectly does on PostgreSQL when the script is still referenced as a type script but not as a lock script. [3](#0-2) 

---

Audit Report

## Title
Copy-Paste Bug in `script_exists_in_output` Causes Type Script Deletion During Rollback on PostgreSQL — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary

In `script_exists_in_output`, after querying whether a script is referenced as a `type_script_id` in any surviving output, the final `match` at line 252 reads `row_lock` instead of `row_type`. On PostgreSQL, this causes the function to return the lock-query result for the type-query check, making it return `false` for any type script that is not simultaneously used as a lock script — even if surviving outputs still reference it. During `rollback_block`, such scripts are then incorrectly deleted from the `script` table, corrupting the indexer's deduplication invariant and causing subsequent `get_cells`/`get_transactions` queries to return missing or wrong results.

## Finding Description

`script_exists_in_output` issues two SQL EXISTS queries:

1. `row_lock`: `WHERE lock_script_id = $1`
2. `row_type`: `WHERE type_script_id = $1`

After the lock check (lines 223–235), `row_type` is fetched (lines 237–249). The final match at line 252 should read `row_type.try_get::<bool, _>(0)` but instead reads `row_lock.try_get::<bool, _>(0)`. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL returns BOOLEAN for EXISTS), so `Ok(r)` is returned where `r` is the stale lock-query result. The `row_type` result is silently discarded. On SQLite, `try_get::<bool, _>(0)` fails (BIGINT), so the `Err(_)` branch correctly reads `row_type.get::<i64, _>(0)` — SQLite is unaffected.

Concrete corruption path (PostgreSQL only):
1. Block B (surviving) has output O2 with `type_script_id = T1`; T1 is not used as any `lock_script_id`.
2. Block A (rolled back) also has an output with `type_script_id = T1`.
3. After O1 is removed, `script_exists_in_output(T1)` is called.
4. `row_lock` query: `lock_script_id = T1` → `false`. `row_type` query: `type_script_id = T1` → `true` (O2 still references it).
5. Bug: `row_lock.try_get::<bool, _>(0)` returns `Ok(false)` → function returns `false`.
6. T1 is pushed to `script_id_list_to_remove` and deleted from the `script` table.
7. O2 now has a dangling `type_script_id` foreign key; all `get_cells`/`get_transactions` queries joining `output` with `script` on this ID return missing results.

## Impact Explanation

This is a concrete data corruption in the rich indexer's state storage. The `script` table is the deduplication table for all scripts referenced by outputs; incorrect deletion breaks the join between `output` and `script`, causing the indexer's RPC APIs to return silently wrong results for any cell or transaction referencing the affected type script. This matches **Medium — Suboptimal implementation of CKB state storage mechanism** (2001–10000 points): the rich indexer is a CKB state storage mechanism, and the corruption is non-hypothetical, triggered by normal chain operation.

## Likelihood Explanation

No attacker action is required. Chain reorganizations are a normal, non-adversarial event in CKB. Any reorg where a rolled-back block and a surviving block share a type script, and that type script is not simultaneously used as a lock script in any surviving output, will trigger the corruption on any PostgreSQL-backed rich indexer deployment. This is a realistic and repeatable condition.

## Recommendation

Fix line 252 to read `row_type` instead of `row_lock`:

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
```

## Proof of Concept

1. Start a CKB node with a PostgreSQL-backed rich indexer.
2. Index Block A containing a transaction with output O1: `lock_script_id = L1`, `type_script_id = T1` (T1 is not used as any lock script in any block).
3. Index Block B (on a different branch) containing output O2: `lock_script_id = L2`, `type_script_id = T1`.
4. Trigger a reorg that rolls back Block A (keeping Block B).
5. Query the `script` table: T1 is absent despite O2 still referencing it.
6. Call `get_cells` filtering by T1's script hash: returns empty, even though O2 is a live unspent output.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L33-38)
```rust
        if let Some(type_script_id) = type_script_id
            && !script_exists_in_output(type_script_id, tx).await?
        {
            script_id_list_to_remove.push(type_script_id);
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
