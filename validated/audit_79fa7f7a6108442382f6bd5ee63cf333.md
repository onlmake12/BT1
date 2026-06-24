Audit Report

## Title
PostgreSQL-Only Copy-Paste Bug in `script_exists_in_output` Causes Incorrect Script Deletion During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the final `match` block at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this causes the function to always return `false` for any script used only as a `type_script_id` in surviving outputs, because `row_lock` already returned `false` to reach that point. During `rollback_block`, such scripts are incorrectly deleted from the `script` table, permanently corrupting the indexer until a full resync.

## Finding Description
`script_exists_in_output` (lines 204–257) performs two SQL `EXISTS` queries: one for `lock_script_id` (stored in `row_lock`, lines 208–220) and one for `type_script_id` (stored in `row_type`, lines 237–249). The first match block (lines 223–235) correctly short-circuits on a `true` lock result. Execution only reaches line 252 when `row_lock` returned `false`. The second match block at line 252 then reads:

```rust
match row_lock.try_get::<bool, _>(0) {   // BUG: should be row_type
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds (BOOLEAN), returning `Ok(false)` — the same value already established. `row_type` is never read. On SQLite, `try_get::<bool, _>` fails (BIGINT), so the `Err(_)` arm correctly reads `row_type`. `rollback_block` (lines 7–52) calls `script_exists_in_output` for each deleted output's `type_script_id` (lines 33–37). The broken function returns `false` for any script used only as `type_script_id` in surviving outputs, so those scripts are pushed to `script_id_list_to_remove` and deleted at line 39, leaving dangling foreign-key references in the `output` table.

## Impact Explanation
This is a confirmed incorrect implementation of the CKB rich-indexer state storage mechanism (Medium: 2001–10000 points). After the incorrect deletion, the `output` table retains rows with `type_script_id` pointing to deleted `script` rows. Any RPC query joining `output` with `script` on `type_script_id` (e.g., `get_cells`, `get_transactions`, `get_cells_capacity` filtered by type script) returns zero results for affected cells. The corruption is permanent until the indexer is fully reset and resynced from genesis.

## Likelihood Explanation
The trigger is a natural blockchain reorg — a routine event requiring no attacker action. The only preconditions are a PostgreSQL-backed rich indexer deployment (explicitly documented and supported) and a rolled-back block containing outputs whose type script is not also used as a lock script in surviving outputs, which is the common case for UDT/NFT type scripts. The bug is invisible in the default SQLite deployment, meaning it goes undetected until a PostgreSQL operator experiences a reorg.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // fix: was row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Additionally, deduplicate `script_id_list_to_remove` before calling `remove_batch_by_blobs` to avoid redundant deletions when multiple outputs in the same block share a script ID.

## Proof of Concept
1. Start a CKB node with `db_type = "postgres"` in `[indexer_v2.rich_indexer]`.
2. Index block A (height N−1) with output O₀: `type_script_id = S`, `lock_script_id = L₁` (L₁ ≠ S).
3. Index block B (height N) with outputs O₁, O₂: `type_script_id = S`, `lock_script_id = L₂` (L₂ ≠ S).
4. Trigger rollback of block B (simulate a reorg).
5. **Expected:** script S survives — O₀ in block A still references it as `type_script_id`.
6. **Actual (PostgreSQL):** `script_exists_in_output(S)` checks only `lock_script_id = S` (finds nothing), returns `false`, S is deleted. O₀ now has a dangling `type_script_id`.
7. Query `get_cells` with `type_script = S` → returns 0 results instead of 1.
8. Repeat with SQLite → returns 1 result (correct), confirming the differential. [1](#0-0) [2](#0-1)

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L29-39)
```rust
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
