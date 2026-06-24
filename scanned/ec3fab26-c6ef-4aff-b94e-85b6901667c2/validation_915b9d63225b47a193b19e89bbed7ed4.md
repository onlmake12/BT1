The bug is confirmed in the actual code. Line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`, exactly as claimed.

Audit Report

## Title
Wrong Variable Reference in `script_exists_in_output()` Causes Premature Script Deletion During Reorg — (File: util/rich-indexer/src/indexer/remove.rs)

## Summary
`script_exists_in_output` in `remove.rs` contains a copy-paste error at line 252: the second `match` block re-reads `row_lock` (the lock-script EXISTS result) instead of `row_type` (the type-script EXISTS result). On PostgreSQL, where `try_get::<bool, _>(0)` succeeds, the function silently returns the lock-script existence result for both checks, causing `rollback_block` to incorrectly delete script rows that are still referenced as `type_script_id` by surviving outputs, permanently corrupting the rich-indexer database on every reorg involving type-scripted cells.

## Finding Description
`script_exists_in_output` (lines 204–257) runs two SQL `EXISTS` queries: one against `lock_script_id` (result stored in `row_lock`) and one against `type_script_id` (result stored in `row_type`). The first `match` block at lines 223–235 correctly uses `row_lock` and short-circuits with `Ok(true)` if the lock-script query matched. The second `match` block at lines 252–256 is supposed to evaluate `row_type`, but instead reads `row_lock` again:

```rust
// line 252 — BUG: row_lock used instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL returns `BOOLEAN`), so the `Ok(r)` arm is taken and `row_type` is never consulted. The function returns the lock-script result for both the lock and type checks. On SQLite, `try_get::<bool, _>` fails (SQLite returns `BIGINT`), so the `Err` arm falls through to `row_type.get::<i64, _>(0)`, which is correct — SQLite is unaffected.

`rollback_block` (lines 29–38) calls `script_exists_in_output` for both `lock_script_id` and `type_script_id` of each rolled-back output, and pushes any ID for which the function returns `false` into `script_id_list_to_remove`, which is then deleted from the `script` table. Because the function returns the lock-script result for the type-script check on PostgreSQL, any script used only as a `type_script_id` (not simultaneously as a `lock_script_id` in a surviving output) is incorrectly reported as absent and deleted. Subsequent RPC queries joining on `script_id` (e.g., `get_cells`, `get_transactions`) return incomplete or missing results for any cell whose type script was incorrectly purged.

No existing guard prevents this: the `remove_batch_by_blobs` call at line 39 executes unconditionally on whatever IDs were accumulated, and the SQL `EXISTS` queries themselves are correct — the error is purely in which result variable is read.

## Impact Explanation
This is a concrete instance of incorrect implementation of the CKB state storage mechanism (the rich-indexer). The rich-indexer is the designated CKB state indexing and storage service; its `script` table is the authoritative source for script data used by indexer RPC methods. Silent, permanent deletion of valid script rows on every PostgreSQL-backed reorg involving type-scripted cells (UDT, NFT, DAO, etc.) constitutes a suboptimal/incorrect implementation of the CKB state storage mechanism, matching the **Medium (2001–10000 points)** allowed impact class.

## Likelihood Explanation
Chain reorganizations are routine and externally triggerable by any peer relaying a competing chain of sufficient work — no special privilege is required. Type scripts are ubiquitous on CKB (UDT, NFT, DAO, etc.). Any PostgreSQL-backed rich-indexer node that experiences a reorg involving type-scripted cells will silently corrupt its database. The trigger path is: external peer delivers heavier chain → `rollback_block` is called → `script_exists_in_output` returns wrong result for type scripts → script rows deleted incorrectly. This is repeatable on every such reorg.

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
1. Set up a PostgreSQL-backed rich-indexer node and sync it to a height with outputs that have type scripts (e.g., any UDT or DAO cell).
2. Trigger a chain reorganization (e.g., by feeding the node a competing chain of greater work that rolls back a block containing type-scripted outputs).
3. Observe that `rollback_block` is called, which calls `script_exists_in_output(type_script_id, tx)`.
4. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` at line 252 succeeds and returns the lock-script EXISTS result (which is `false` for a script used only as a type script), causing the type script ID to be added to `script_id_list_to_remove`.
5. The script row is deleted from the `script` table even though surviving outputs still reference it via `type_script_id`.
6. Subsequent `get_cells` or `get_transactions` RPC calls for cells with that type script return incomplete or missing results, confirming database corruption.

A targeted unit test can be written by: (a) inserting a script row used only as `type_script_id`, (b) calling `script_exists_in_output` against a PostgreSQL connection, and (c) asserting the return value is `true` — on the buggy code it returns `false`. [1](#0-0) [2](#0-1)

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
