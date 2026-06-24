The code at line 252 confirms the claim exactly. [1](#0-0) 

Line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`, and the deletion path at lines 33–37 confirms the impact. [2](#0-1) 

---

Audit Report

## Title
Wrong Variable Reference in `script_exists_in_output` Causes Script Deletion During Block Rollback on PostgreSQL - (File: util/rich-indexer/src/indexer/remove.rs)

## Summary
In `script_exists_in_output`, after fetching `row_type` (the EXISTS result for `type_script_id`), the final match on line 252 reads `row_lock` instead of `row_type`. On PostgreSQL, this causes the function to always return `Ok(false)` for any type-script check, so every type-script-only script ID is unconditionally deleted from the `script` table during `rollback_block`, corrupting the Rich Indexer's relational state.

## Finding Description
`script_exists_in_output` issues two SQL `EXISTS` queries: one for `lock_script_id` (result stored in `row_lock`) and one for `type_script_id` (result stored in `row_type`).

After the first query, if `row_lock` is `true` the function returns early (`Ok(true)`). This means any code that reaches line 252 has already established that `row_lock` holds `false`.

The second query stores its result in `row_type`, but the final match at line 252 reads `row_lock` again:

```rust
// line 252 — BUG: should be row_type, not row_lock
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),          // r is always false here on PostgreSQL
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds and returns `false` (the early-return guard above guarantees this). The `Ok(r)` arm therefore always returns `Ok(false)`, completely ignoring `row_type`. On SQLite, `try_get::<bool, _>` fails on a `BIGINT` column, so the `Err` branch is taken and `row_type` is read correctly — SQLite is unaffected.

`rollback_block` calls `script_exists_in_output` for every `type_script_id` in the rolled-back outputs and deletes all IDs for which it returns `false`:

```rust
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
```

Because the function always returns `false` for type scripts on PostgreSQL, every such script ID is pushed to `script_id_list_to_remove` and deleted, even when the script is still referenced by surviving outputs.

## Impact Explanation
This is a concrete incorrect implementation of the CKB Rich Indexer's state storage mechanism. During any block rollback, script rows still referenced by surviving outputs via `type_script_id` are deleted from the `script` table. Subsequent Rich Indexer RPC calls (`get_cells`, `get_transactions`) filtering by type script return incorrect or empty results. If foreign-key constraints are enforced, subsequent inserts or queries may produce database errors. Full re-indexing from genesis is required to recover. This matches **Medium (2001–10000 points): Suboptimal/incorrect implementation of CKB state storage mechanism**.

## Likelihood Explanation
Block rollbacks occur during any chain reorganization, which is a routine event on a live network. No attacker action is required — any reorg of depth ≥ 1 involving outputs with type scripts triggers the faulty path. The bug is deterministic and reproducible on any PostgreSQL-backed Rich Indexer deployment that experiences a reorg. PostgreSQL is the recommended backend for production deployments.

## Recommendation
On line 252, replace `row_lock` with `row_type`:

```rust
// BEFORE (buggy):
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}

// AFTER (correct):
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

## Proof of Concept
1. Run a CKB node with the Rich Indexer configured to use PostgreSQL.
2. Submit a transaction whose output has a `type_script` (e.g., a Simple UDT cell). Let it be included in block N.
3. Trigger a chain reorganization that rolls back block N.
4. `rollback_block` calls `script_exists_in_output(type_script_id, tx)`.
5. At line 252, `row_lock.try_get::<bool, _>(0)` returns `Ok(false)` (PostgreSQL path), so the function returns `Ok(false)` regardless of `row_type`.
6. The type script ID is added to `script_id_list_to_remove` and deleted from the `script` table.
7. Subsequent `get_cells` RPC calls filtering by that type script return empty results, confirming database corruption.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L33-39)
```rust
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
