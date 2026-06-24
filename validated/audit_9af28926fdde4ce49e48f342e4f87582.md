The code at line 252 is confirmed exactly as claimed: [1](#0-0) 

After fetching `row_type` (the `type_script_id` EXISTS query), the final match block reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds, so the function returns the lock-script result for both checks. On SQLite, `try_get::<bool, _>` fails and the fallback correctly reads `row_type`. The rollback path at lines 29–38 relies on this function to decide which scripts to delete. [2](#0-1) 

---

Audit Report

## Title
Copy-Paste Bug in `script_exists_in_output` Returns Lock-Script Result for Type-Script Check on PostgreSQL, Causing Silent Script Deletion During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output` (line 252), the final `match` block reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this causes the function to return the lock-script EXISTS result for both the lock and type checks. During rollback, any script referenced only as a `type_script` in a surviving block's output is incorrectly reported as unreferenced and permanently deleted from the `script` table, corrupting the indexer's live-cell state.

## Finding Description
`script_exists_in_output` issues two SQL `EXISTS` queries: `row_lock` (`WHERE lock_script_id = $1`) and `row_type` (`WHERE type_script_id = $1`). The first match block (lines 223–235) correctly short-circuits and returns `true` if the lock query is true. The second match block (lines 252–256) is supposed to evaluate `row_type` but instead re-reads `row_lock`:

```rust
// line 252 — BUG: should be row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `EXISTS` returns a `BOOLEAN`, so `row_lock.try_get::<bool, _>(0)` always succeeds (`Ok(r)`), and `r` is the lock-script result — not the type-script result. On SQLite, `EXISTS` returns `BIGINT`, so `try_get::<bool, _>` fails and the fallback correctly reads `row_type`, making SQLite unaffected.

The rollback path in `rollback_block` (lines 29–38) removes the rolled-back block's outputs first, then calls `script_exists_in_output` for each script. If script `S` is used as `type_script` in a surviving block's output (but not as `lock_script`):
- `WHERE lock_script_id = S.id` → `false` (no surviving output uses it as lock)
- `WHERE type_script_id = S.id` → `true` (surviving output still references it)
- Bug: function returns `false` (the lock result) → `S` is pushed to `script_id_list_to_remove` and deleted

The surviving output row retains `type_script_id = S.id`, but the `script` row no longer exists. All RPC queries joining `output` to `script` for that script hash return empty results.

## Impact Explanation
This is a concrete implementation bug in the CKB rich-indexer's state storage mechanism. It causes permanent, silent corruption of the `script` table in PostgreSQL deployments during any chain reorg where a rolled-back block shares a `type_script` with a surviving block. Subsequent RPC calls (`get_cells`, `get_cells_capacity`) filtered by that script return empty results, permanently misrepresenting live-cell state. This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation
Reorgs are a normal, unprivileged part of CKB chain operation requiring no attacker action. Any PostgreSQL-backed rich-indexer deployment that experiences a reorg where the rolled-back block shares a `type_script` with a surviving block (e.g., any two blocks whose transactions use the same UDT, NFT, or other type script) will silently corrupt its `script` table. This condition is common in practice and requires no special attacker capability.

## Recommendation
Fix line 252 in `remove.rs` to read `row_type` instead of `row_lock`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // ← was row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

## Proof of Concept
1. Start a PostgreSQL-backed rich-indexer node.
2. Index block 1 containing a transaction with an output whose `type_script` is `S` and `lock_script` is `L ≠ S`.
3. Index block 2 containing a transaction with an output also using `S` as `type_script` (`ON CONFLICT DO NOTHING` skips re-inserting `S`).
4. Trigger rollback of block 2 (simulate a reorg by calling `rollback_block`).
5. Query: `SELECT * FROM script WHERE id = <S.id>` — **expected**: row exists; **observed**: row is gone.
6. Call RPC `get_cells` filtered by script `S` — **expected**: returns block 1's cell; **observed**: returns empty.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L29-38)
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
