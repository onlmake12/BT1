The bug at line 252 is confirmed in the actual source code.

Audit Report

## Title
Copy-Paste Bug in `script_exists_in_output` Causes Incorrect Script Deletion on PostgreSQL During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary

In `script_exists_in_output`, line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this causes the function to always return `false` for the type-script existence check, because `row_lock` is already known to be `false` at that point (the early-return at lines 223–227 would have fired otherwise). As a result, every rollback on a PostgreSQL-backed rich-indexer incorrectly deletes script rows that are still referenced by surviving outputs as type scripts, permanently corrupting the indexer's state for those cells.

## Finding Description

The function `script_exists_in_output` (lines 204–257 of `util/rich-indexer/src/indexer/remove.rs`) first queries whether `script_id` appears as a `lock_script_id` in any surviving output row, storing the result in `row_lock`. If `row_lock` is true it returns early. If not, it queries whether `script_id` appears as a `type_script_id`, storing the result in `row_type`. The final `match` at line 252 is supposed to evaluate `row_type`, but instead re-evaluates `row_lock`:

```rust
// line 252 — copy-paste bug: row_lock used instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),                              // PostgreSQL always takes this arm
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1), // SQLite takes this arm (correct)
}
```

On PostgreSQL, `EXISTS` returns `BOOLEAN`, so `row_lock.try_get::<bool, _>(0)` always succeeds with `Ok(false)` (since `true` would have triggered the early return). The function therefore always returns `Ok(false)` for any script that is referenced only as a `type_script_id`, regardless of the actual query result stored in `row_type`.

The caller `rollback_block` (lines 28–39) uses this return value to decide which script IDs to hard-delete:

```rust
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
```

Because `script_exists_in_output` always returns `false` for type-only scripts on PostgreSQL, every such script from the rolled-back block is pushed into `script_id_list_to_remove` and deleted, even when surviving outputs still hold a foreign key reference to it via `type_script_id`.

On SQLite, `EXISTS` returns `BIGINT`, so `row_lock.try_get::<bool, _>(0)` returns `Err(_)`, and the fallthrough correctly reads `row_type`. SQLite is unaffected.

All existing rollback tests (`util/rich-indexer/src/tests/rollback.rs`) use `connect_sqlite(MEMORY_DB)` and therefore do not exercise the PostgreSQL code path, leaving this bug invisible to the current test suite.

## Impact Explanation

This is a concrete correctness bug in the CKB rich-indexer's state storage mechanism. After the script row for a type-only script S is deleted, the `output` rows for surviving cells still hold `type_script_id = <deleted id>`. Every `get_cells` query that joins `output` to `script` on `type_script_id` returns zero results for those live cells. The cells become permanently invisible to type-script queries until a full re-index is performed. This constitutes a suboptimal (incorrect) implementation of the CKB state storage mechanism, matching the **Medium (2001–10000 points)** bounty impact class.

## Likelihood Explanation

- Affects all PostgreSQL-backed rich-indexer deployments.
- Triggered by any 1-block chain reorganization, which is a routine network event requiring no attacker action.
- The rolled-back block need only contain one output whose type script is also referenced by a surviving output — a common pattern for UDT or NFT transactions.
- No special privileges, keys, or external conditions are required.

## Recommendation

Change line 252 from `row_lock` to `row_type`:

```rust
// Before (buggy):
match row_lock.try_get::<bool, _>(0) {

// After (correct):
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Add a PostgreSQL integration test that appends two blocks both containing an output with the same type-only script, rolls back the second block, and asserts the script row still exists and `get_cells` filtered by that type script still returns the surviving cell.

## Proof of Concept

1. Start a PostgreSQL-backed rich-indexer.
2. Append **block0** with one output: `lock = L1, type = S`.
3. Append **block1** with one output: `lock = L2, type = S` (S reused as type only).
4. Call `rollback()` to remove block1.
5. Query `SELECT id FROM script WHERE …` for S.
6. **Expected**: S still exists (referenced by block0's surviving output).
7. **Actual on PostgreSQL**: S is deleted. Subsequent `get_cells` filtered by S as type script returns an empty list even though the cell from block0 is live.