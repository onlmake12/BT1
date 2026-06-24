Audit Report

## Title
Copy-Paste Bug in `script_exists_in_output` Reads `row_lock` Instead of `row_type` on PostgreSQL, Causing Type Script Corruption During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary

At line 252 of `remove.rs`, the second `match` block in `script_exists_in_output` reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, `try_get::<bool, _>(0)` always succeeds (PostgreSQL returns `BOOLEAN` for `EXISTS`), so the function returns the already-evaluated `row_lock` result — which is guaranteed `false` at that point — instead of the `row_type` result. This causes every type script from a rolled-back block to be unconditionally deleted from the `script` table, even when surviving outputs still reference it, leaving the indexer database in a permanently inconsistent state.

## Finding Description

`script_exists_in_output` executes two SQL `EXISTS` queries:

- `row_lock` (lines 208–220): checks `WHERE lock_script_id = $1`
- `row_type` (lines 237–249): checks `WHERE type_script_id = $1`

The first match block (lines 223–235) correctly short-circuits and returns `Ok(true)` if `row_lock` is `true`. Execution only reaches line 252 when `row_lock` is `false`. The second match block then re-reads `row_lock` instead of `row_type`:

```rust
// line 252 — BUG: row_lock used instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),                          // always Ok(false) on PostgreSQL
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),  // SQLite path — correct
}
``` [1](#0-0) 

On PostgreSQL, `try_get::<bool, _>(0)` succeeds and returns `false` (the already-known `row_lock` value), so `row_type` is never consulted. On SQLite, `try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err` arm correctly reads `row_type`. SQLite is unaffected.

The caller in `rollback_block` uses this result to decide whether to delete a script:

```rust
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
``` [2](#0-1) 

Because `script_exists_in_output` always returns `false` for the type-script check on PostgreSQL, every type script from the rolled-back block is unconditionally added to `script_id_list_to_remove` and deleted via `remove_batch_by_blobs("script", ...)`, even when other surviving outputs still reference it via `type_script_id`. [3](#0-2) 

## Impact Explanation

This is a **Medium** impact: suboptimal/incorrect implementation of the CKB state storage mechanism (the rich-indexer). After any reorg on a PostgreSQL-backed rich-indexer node, the `script` table loses rows still referenced by the `output` table via `type_script_id`. The indexer database is left in a permanently inconsistent state — subsequent RPC queries filtered by type script (`get_cells`, `get_transactions`) return incomplete or missing results until a full re-sync. This is a concrete, deterministic corruption of the CKB state storage layer.

## Likelihood Explanation

- Chain reorgs are a normal, unprivileged blockchain event — any competing chain tip triggers `rollback_block`.
- PostgreSQL is a supported and documented backend for the rich-indexer.
- The bug is deterministic: every reorg on PostgreSQL involving outputs with type scripts will trigger it.
- No special attacker capability is required; a natural reorg suffices.

## Recommendation

Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// Fix:
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [1](#0-0) 

## Proof of Concept

1. Run a PostgreSQL-backed rich-indexer node.
2. Submit a block containing a transaction with at least one output that has a type script. Ensure another surviving output (in a different block) shares the same `type_script_id`.
3. Trigger a reorg that rolls back that block (`rollback_block` is called).
4. Query the `script` table: the type script row will be absent.
5. Query the `output` table: surviving outputs still reference the deleted `type_script_id` via foreign key.
6. Issue an RPC call filtered by that type script — results are empty/incorrect despite the outputs existing.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L33-37)
```rust
        if let Some(type_script_id) = type_script_id
            && !script_exists_in_output(type_script_id, tx).await?
        {
            script_id_list_to_remove.push(type_script_id);
        }
```

**File:** util/rich-indexer/src/indexer/remove.rs (L39-39)
```rust
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
