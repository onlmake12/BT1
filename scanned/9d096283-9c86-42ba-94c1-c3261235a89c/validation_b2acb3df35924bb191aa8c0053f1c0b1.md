The code at line 252 is confirmed. The bug is exactly as described — `row_lock.try_get::<bool, _>(0)` is used instead of `row_type.try_get::<bool, _>(0)`.

Audit Report

## Title
`script_exists_in_output` Uses Wrong Row Variable on PostgreSQL, Silently Discarding Type-Script Existence Check During Reorg — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
A copy-paste error at line 252 of `script_exists_in_output` causes the function to re-read `row_lock` (the lock-script EXISTS result) instead of `row_type` (the type-script EXISTS result) when running against PostgreSQL. Because execution only reaches line 252 when `row_lock` is already `false`, the match always returns `Ok(false)` on PostgreSQL, and the `row_type` query result is silently discarded. During `rollback_block`, every type script in the rolled-back block's outputs is therefore unconditionally added to the deletion list, even if that script is still referenced by live outputs in earlier blocks.

## Finding Description
`script_exists_in_output` (line 204) performs two `SELECT EXISTS` queries: one for `lock_script_id` (stored in `row_lock`, lines 208–220) and one for `type_script_id` (stored in `row_type`, lines 237–249). After the lock query, if the result is `true` the function returns early (lines 225–227). Execution only continues to the type query when `row_lock` is `false`.

The final match block at line 252 then reads:

```rust
match row_lock.try_get::<bool, _>(0) {   // BUG: should be row_type
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `EXISTS` returns a native `BOOLEAN`, so `row_lock.try_get::<bool, _>(0)` succeeds and yields `Ok(false)` — the only value it can have at this point, since `true` already caused an early return. The `row_type` variable is fetched from the database but never read on PostgreSQL. On SQLite, `EXISTS` returns `BIGINT`, so `try_get::<bool, _>(0)` fails, the `Err(_)` branch fires, and `row_type.get::<i64, _>(0)` is correctly consulted — making this bug PostgreSQL-only.

`rollback_block` (lines 28–39) calls `script_exists_in_output` for every type script in the rolled-back block's outputs and deletes all scripts for which it returns `false`. Because the function always returns `false` for the type-script path on PostgreSQL, every type script from the rolled-back block is deleted from the `script` table, regardless of whether it is still referenced by outputs in retained blocks. [1](#0-0) [2](#0-1) [3](#0-2) 

## Impact Explanation
This is a concrete corruption of the rich-indexer's state storage: the `script` table loses rows that are still semantically live. All subsequent `get_cells` / `get_transactions` RPC calls that filter by an affected type script return zero results, because the script row no longer exists to join against. This maps to **Suboptimal (incorrect) implementation of CKB state storage mechanism** (Medium, 2001–10000 points). High-value shared type scripts such as the Nervos DAO script or UDT type scripts are realistic targets: a single reorg touching one of their outputs causes all earlier deposits/cells indexed under that script to become invisible to wallets and tooling.

## Likelihood Explanation
No attacker capability is required. Reorgs are a normal, unprivileged consensus event — any competing fork of sufficient work triggers one. Any PostgreSQL-backed rich-indexer node that experiences a reorg where the rolled-back block contains a type script also present in earlier retained outputs will silently corrupt its index. The condition is not rare: DAO deposits and UDT outputs routinely span many blocks, and short reorgs (1–2 blocks) occur in normal CKB operation.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // was: row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [1](#0-0) 

## Proof of Concept
1. Start a PostgreSQL-backed rich-indexer node.
2. Index block A containing an output with the DAO type script (lock + type).
3. Index block B (tip) containing another output with the same DAO type script.
4. Trigger a reorg that rolls back block B (e.g., by feeding a competing chain of equal or greater work).
5. `rollback_block` removes block B's outputs, then calls `script_exists_in_output(dao_type_script_id, tx)`.
6. The function queries `row_type` (EXISTS in remaining outputs → `true`), but at line 252 reads `row_lock` instead, returning `Ok(false)`.
7. The DAO type script id is pushed into `script_id_list_to_remove` and deleted.
8. Query `get_cells` with a type-script filter for the DAO script.
9. **Expected**: DAO deposit cell from block A is returned.
10. **Actual**: Zero results — the script row was deleted.

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
