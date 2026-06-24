The bug is confirmed in the actual code at line 252. [1](#0-0) 

At line 252, `row_lock.try_get::<bool, _>(0)` is used in the final `match` block instead of `row_type.try_get::<bool, _>(0)`. The `row_type` variable is fetched but its boolean value is never read on PostgreSQL. The call site in `rollback_block` and the `script_id_list_to_remove` logic are also confirmed. [2](#0-1) 

---

Audit Report

## Title
`script_exists_in_output` Re-Checks `row_lock` Instead of `row_type`, Silently Skipping the Type-Script Existence Check on PostgreSQL During Block Rollback — (File: util/rich-indexer/src/indexer/remove.rs)

## Summary
In `script_exists_in_output`, the final `match` block at line 252 evaluates `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, where `EXISTS` returns a native `BOOLEAN`, the `Ok(r)` branch always succeeds, causing the function to return the lock-script query result a second time and completely ignore the type-script query result. During `rollback_block`, any script referenced exclusively as a `type_script_id` is incorrectly reported as absent and deleted from the `script` table, permanently corrupting the rich indexer's relational state.

## Finding Description
`script_exists_in_output` (lines 204–257) executes two `SELECT EXISTS` queries: one against `lock_script_id` into `row_lock`, one against `type_script_id` into `row_type`. After the second query, the final `match` at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, `try_get::<bool, _>(0)` succeeds (column type is `BOOLEAN`), so the `Ok(r)` arm is always taken and returns the stale lock-script result. `row_type` is fetched but its value is never consumed. On SQLite, `try_get::<bool, _>` fails (column type is `BIGINT`), the `Err(_)` arm is taken, and `row_type.get::<i64, _>(0)` is correctly read — SQLite is unaffected. The caller `rollback_block` (lines 28–39) iterates over `output_lock_type_list`, calls `script_exists_in_output` for each `type_script_id`, and pushes any ID returning `false` into `script_id_list_to_remove`, which is then deleted via `remove_batch_by_blobs`. Because the function always returns the lock-script result for the type-script check, any script used only as a type script is pushed into the removal list and deleted even while live outputs still reference it.

## Impact Explanation
This matches **"Suboptimal implementation of CKB state storage mechanism" (Medium, 2001–10000 points)**. The rich indexer's PostgreSQL `script` table is silently corrupted on every reorg involving type-scripted outputs (DAO deposits, UDT cells, NFTs). Subsequent RPC calls (`get_cells`, `get_transactions`, `get_cells_capacity`) filtering by the deleted type script return empty or incorrect results. The corruption is persistent and requires a full re-index to repair. This does not rise to Critical/High because it affects only the supplementary rich indexer component, not core consensus or node stability.

## Likelihood Explanation
Chain reorganizations are a normal, expected event in CKB and require no attacker capability — any peer submitting a valid competing chain with more accumulated work triggers a reorg. The sync loop in `util/indexer-sync/src/lib.rs` calls `indexer.rollback()` on every detected reorg. Type-scripted outputs (DAO, UDT, NFT) are extremely common on mainnet. Any operator running the rich indexer with a PostgreSQL backend is affected by every reorg that touches type-scripted outputs, making this highly repeatable and practically certain to occur in production.

## Recommendation
Replace `row_lock` with `row_type` in the final `match` block of `script_exists_in_output` at line 252:

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

Add a PostgreSQL-backed integration test for `rollback_block` asserting that a script referenced exclusively as a `type_script_id` in remaining outputs is not deleted after rollback.

## Proof of Concept
1. Using a PostgreSQL backend, append a block with a transaction whose output has `lock_script_id = A`, `type_script_id = B` (script B is not used as a lock script anywhere).
2. Append a second block spending that output and creating a new output with `lock_script_id = C`, `type_script_id = B`.
3. Trigger rollback of block 2 (simulating a reorg).
4. **Expected:** `script_exists_in_output(B, tx)` returns `true`; script B remains in the `script` table.
5. **Actual:** `script_exists_in_output(B, tx)` executes the lock query (no rows → `false`), executes the type query into `row_type` (finds a row → would be `true`), but then evaluates `row_lock.try_get::<bool, _>(0)` which returns `Ok(false)`. The function returns `false`. Script B is deleted. Subsequent `get_cells` filtered by script B returns empty results despite live outputs referencing it.

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

**File:** util/rich-indexer/src/indexer/remove.rs (L251-256)
```rust
    // pg type is BOOLEAN
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```
