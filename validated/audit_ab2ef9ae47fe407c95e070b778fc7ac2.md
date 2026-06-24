The code at line 252 is confirmed exactly as described. [1](#0-0) 

The early-return logic at lines 223–235 means that by the time execution reaches line 252, `row_lock` is definitively `false` on PostgreSQL (if it were `true`, the function would have returned `Ok(true)` at line 226). So `row_lock.try_get::<bool, _>(0)` at line 252 always returns `Ok(false)` on PostgreSQL, silently discarding `row_type`. [2](#0-1) 

The rollback path at lines 33–37 calls `script_exists_in_output` for each type script and deletes it if the function returns `false`. [3](#0-2) 

---

Audit Report

## Title
Copy-Paste Bug in `script_exists_in_output` Returns Stale Lock-Query Result for Type-Script Check, Causing Incorrect Script Deletion During Rollback on PostgreSQL — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary

In `script_exists_in_output`, after querying whether a script is referenced as a `type_script_id` in any surviving output, the final `match` at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this causes the function to always return `false` for any script that is not simultaneously used as a lock script, even when surviving outputs still reference it as a type script. During `rollback_block`, such scripts are then incorrectly deleted from the `script` table, corrupting the indexer's deduplication invariant and causing `get_cells`/`get_transactions` queries to return missing results.

## Finding Description

`script_exists_in_output` issues two SQL EXISTS queries:

1. `row_lock`: `WHERE lock_script_id = $1` (lines 208–220)
2. `row_type`: `WHERE type_script_id = $1` (lines 237–249)

After the lock check (lines 223–235), if `row_lock` is `true`, the function returns `Ok(true)` early. If `row_lock` is `false`, execution falls through to fetch `row_type`. The final match at line 252 should read `row_type.try_get::<bool, _>(0)` but instead reads `row_lock.try_get::<bool, _>(0)`.

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL returns BOOLEAN for EXISTS) and, because the early-return guard already filtered out the `true` case, always returns `Ok(false)` at this point. The `row_type` result is silently discarded. On SQLite, `try_get::<bool, _>(0)` fails (BIGINT), so the `Err(_)` branch correctly reads `row_type.get::<i64, _>(0) == 1` — SQLite is unaffected.

Concrete corruption path (PostgreSQL only):
1. Block B (surviving) has output O2 with `type_script_id = T1`; T1 is not used as any `lock_script_id` in any surviving output.
2. Block A (rolled back) also has an output with `type_script_id = T1`.
3. `rollback_block` removes Block A's outputs, then calls `script_exists_in_output(T1, tx)`.
4. `row_lock` query: `lock_script_id = T1` → `false`. No early return.
5. `row_type` query: `type_script_id = T1` → `true` (O2 still references it).
6. Bug: `row_lock.try_get::<bool, _>(0)` returns `Ok(false)` → function returns `false`.
7. T1 is pushed to `script_id_list_to_remove` and deleted from the `script` table.
8. O2 now has a dangling `type_script_id` foreign key; all `get_cells`/`get_transactions` queries joining `output` with `script` on this ID return missing results.

## Impact Explanation

This is concrete data corruption in the rich indexer's state storage. The `script` table is the deduplication table for all scripts referenced by outputs; incorrect deletion breaks the join between `output` and `script`, causing the indexer's RPC APIs to return silently wrong results for any cell or transaction referencing the affected type script. This matches **Medium — Suboptimal implementation of CKB state storage mechanism** (2001–10000 points): the rich indexer is a CKB state storage mechanism, and the corruption is non-hypothetical, triggered by normal chain operation (block reorganization).

## Likelihood Explanation

No attacker action is required. Chain reorganizations are a normal, non-adversarial event in CKB. Any reorg where a rolled-back block and a surviving block share a type script, and that type script is not simultaneously used as a lock script in any surviving output, will trigger the corruption on any PostgreSQL-backed rich indexer deployment. This is a realistic and repeatable condition requiring no special privileges.

## Recommendation

Fix line 252 to read `row_type` instead of `row_lock`:

```rust
// Before (buggy):
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}

// After (correct):
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
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

**File:** util/rich-indexer/src/indexer/remove.rs (L33-37)
```rust
        if let Some(type_script_id) = type_script_id
            && !script_exists_in_output(type_script_id, tx).await?
        {
            script_id_list_to_remove.push(type_script_id);
        }
```

**File:** util/rich-indexer/src/indexer/remove.rs (L223-235)
```rust
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

**File:** util/rich-indexer/src/indexer/remove.rs (L252-256)
```rust
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```
