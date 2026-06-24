The code at line 252 is confirmed: [1](#0-0) 

Line 252 reads `row_lock.try_get::<bool, _>(0)` while `row_type` was fetched at lines 237–249 and is only used in the `Err` fallback at line 255. The first match block at lines 223–235 correctly reads `row_lock`. The second block at line 252 should read `row_type` but does not. [2](#0-1) 

The `rollback_block` caller deletes outputs before the script-existence check: [3](#0-2) 

All required validation checks pass. The report is valid.

---

Audit Report

## Title
Wrong Variable Reference in `script_exists_in_output()` Causes Premature Script Deletion During Reorg — (File: util/rich-indexer/src/indexer/remove.rs)

## Summary
`script_exists_in_output` at line 252 reads `row_lock` instead of `row_type` in the second `match` block. On PostgreSQL, `try_get::<bool, _>(0)` on `row_lock` always succeeds (PostgreSQL returns `BOOLEAN`), so `row_type` is never consulted and the function returns the lock-script existence result for both checks. `rollback_block` then incorrectly deletes type-script rows from the `script` table during reorgs, silently corrupting the rich-indexer database.

## Finding Description
In `script_exists_in_output` (lines 204–257), two SQL `EXISTS` queries are issued: one for `lock_script_id` (result in `row_lock`, lines 208–220) and one for `type_script_id` (result in `row_type`, lines 237–249). The first `match` at lines 223–235 correctly reads `row_lock`. The second `match` at line 252 also reads `row_lock` instead of `row_type`:

```rust
// line 252 — BUG: should be row_type.try_get::<bool, _>(0)
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `SELECT EXISTS(...)` returns `BOOLEAN`, so `try_get::<bool, _>(0)` on `row_lock` always succeeds and the `Ok(r)` arm is taken — `row_type` is never read. On SQLite, `SELECT EXISTS(...)` returns `BIGINT`, so `try_get::<bool, _>` fails and the `Err` arm falls through to `row_type.get::<i64, _>(0)`, which is correct.

`rollback_block` deletes outputs from the `output` table at line 25 *before* the script-existence check at lines 29–38. After deletion, a script used only as a `type_script_id` will have no matching `lock_script_id` row → `row_lock` returns `false` → the buggy second match re-reads `row_lock` and also returns `false` → the type script ID is pushed to `script_id_list_to_remove` → `remove_batch_by_blobs("script", ...)` at line 39 deletes it, even if other surviving outputs still reference it.

## Impact Explanation
This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**. The rich-indexer is CKB's state storage and indexing layer. Silent deletion of script rows from the `script` table corrupts the indexer's relational state: subsequent RPC calls (`get_cells`, `get_transactions`, etc.) that join on `script_id` return incomplete or missing results for any cell whose type script was incorrectly purged. The corruption is permanent and accumulates across reorgs.

## Likelihood Explanation
Chain reorganizations are a routine, externally-triggerable event requiring no special privilege — any peer relaying a competing chain of sufficient work causes a reorg and invokes `rollback_block`. Type scripts are ubiquitous on CKB (UDT, NFT, DAO, etc.). On any PostgreSQL-backed rich-indexer node, every reorg involving type-scripted cells will trigger the bug. The conditions are common and repeatable.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // ← was row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

## Proof of Concept
1. Deploy a PostgreSQL-backed rich-indexer node.
2. Index a block containing an output with a type script whose `type_script_id` differs from any `lock_script_id` in surviving outputs.
3. Trigger a chain reorganization that rolls back that block (e.g., by feeding a competing chain of greater work).
4. Observe that `rollback_block` calls `script_exists_in_output(type_script_id, tx)`, which returns `false` because `row_lock` for that ID is `false` and the buggy match re-reads `row_lock`.
5. The type script row is deleted from the `script` table.
6. Query `get_cells` or `get_transactions` for a cell referencing that type script — results are missing or incomplete, confirming database corruption.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L25-38)
```rust
    remove_batch_by_blobs("output", "tx_id", &tx_id_list, tx).await?;

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
