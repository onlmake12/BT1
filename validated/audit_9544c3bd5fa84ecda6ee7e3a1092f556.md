The code at line 252 of `util/rich-indexer/src/indexer/remove.rs` confirms the bug exactly as described.

Audit Report

## Title
Wrong Row Variable in `script_exists_in_output` Causes Type Script Deletion on PostgreSQL During Reorg — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary

At line 252 of `script_exists_in_output`, the code reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this causes the function to always return `false` for the type-script existence check, making `rollback_block` unconditionally delete type scripts from the `script` table even when other outputs in earlier blocks still reference them. The `script` table becomes permanently inconsistent with the `output` table until a full re-sync, causing RPC queries filtered by type script to silently return missing or incorrect cells.

## Finding Description

`script_exists_in_output` is a two-phase guard called by `rollback_block` to decide whether a script row can be safely removed after its referencing outputs are deleted.

**Phase 1** (lines 208–235) queries whether `script_id` appears as a `lock_script_id` in any remaining output. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds; if the result is `true`, the function returns early. If `false`, execution falls through.

**Phase 2** (lines 237–256) queries whether `script_id` appears as a `type_script_id` in any remaining output. The result row is stored in `row_type`. However, line 252 reads:

```rust
match row_lock.try_get::<bool, _>(0) {   // BUG: should be row_type
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [1](#0-0) 

Because execution only reaches line 252 after the lock-script check returned `false`, `row_lock` already holds a `false` boolean. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds and returns `Ok(false)`, so the function returns `false` unconditionally — `row_type` is fetched but never consulted. On SQLite, `try_get::<bool, _>` fails (SQLite stores `EXISTS` as `BIGINT`), so the `Err` branch correctly reads `row_type`.

`rollback_block` calls this function for every type script of every output in the rolled-back block:

```rust
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
``` [2](#0-1) 

Because the guard always returns `false` on PostgreSQL, every type script from the rolled-back block is pushed into `script_id_list_to_remove` and deleted: [3](#0-2) 

Outputs in earlier blocks that share the same `type_script_id` now hold a dangling reference to a deleted `script` row.

## Impact Explanation

This matches **Note (0–500 points): Any local RPC API crash/malfunction**. Any RPC query served by the rich-indexer that joins `output` with `script` (e.g., `get_cells` filtered by type script) will silently miss or misreport cells whose type script was incorrectly deleted. The `script` table is permanently inconsistent with the `output` table until the indexer is fully re-synced. This is a concrete, reproducible local RPC API data-integrity failure, not a theoretical issue.

## Likelihood Explanation

Chain reorgs are a normal, unprivileged network event — no attacker capability is required. Any reorg on a PostgreSQL-backed rich-indexer node triggers the bug for every type script that (a) appears in the rolled-back block's outputs and (b) is not simultaneously used as a lock script. This is the common case for application-specific type scripts (e.g., UDT, NFT, DAO). The bug is silent — no error is returned, no log is emitted — so operators may not notice until queries return wrong data.

## Recommendation

Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [1](#0-0) 

## Proof of Concept

1. Start a CKB rich-indexer backed by PostgreSQL.
2. Index Block A containing output O1 with `type_script_id = S1`, where S1 is not used as any lock script.
3. Index Block B containing output O2 also with `type_script_id = S1`.
4. Trigger a reorg that rolls back Block B (`rollback_block` is called).
5. After rollback, query `SELECT * FROM script WHERE id = S1`.
6. **Expected:** S1 is present (O1 still references it).
7. **Actual (PostgreSQL):** S1 is absent — deleted because `script_exists_in_output` returned `false` due to reading `row_lock` instead of `row_type`.
8. Query outputs for O1 via RPC; the type script data is missing or the join returns no row.

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
