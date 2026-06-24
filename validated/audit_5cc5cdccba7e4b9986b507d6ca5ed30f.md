The code at line 252 is confirmed exactly as claimed: [1](#0-0) 

Line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. The early-return guard at lines 223–235 already confirmed `row_lock = false`, so on PostgreSQL (where `EXISTS` returns `BOOLEAN` and `try_get::<bool, _>(0)` succeeds), the function deterministically returns `Ok(false)` without ever consulting `row_type`. [2](#0-1) 

The caller at lines 33–37 then pushes the type script id into `script_id_list_to_remove` and deletes it, even though live outputs still reference it. [3](#0-2) 

All validation checks pass. The report is valid.

---

Audit Report

## Title
Wrong Variable Reference in `script_exists_in_output` Causes Silent Type-Script Deletion on PostgreSQL During Reorgs — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the final `match` block at line 252 reads from `row_lock` instead of `row_type`. On PostgreSQL, `EXISTS` returns `BOOLEAN`, so `row_lock.try_get::<bool, _>(0)` always succeeds and always returns `false` at that point (the early-return guard above would have fired otherwise). The function unconditionally returns `Ok(false)` for any script that is only a type script, causing the caller to delete that script record from the indexer database even though live outputs still reference it.

## Finding Description
`script_exists_in_output` performs two sequential `SELECT EXISTS` queries: one for `lock_script_id` (`row_lock`, lines 208–220) and one for `type_script_id` (`row_type`, lines 237–249). After the lock query, if the result is `true` the function returns early (lines 222–235). If not, it queries `row_type`. The final `match` at line 252 is supposed to return the result of `row_type`, but instead re-reads `row_lock`:

```rust
// line 252 — BUG: row_lock used instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),          // r is always false here on PostgreSQL
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `EXISTS` returns `BOOLEAN`, so `try_get::<bool, _>(0)` always succeeds. Since the early-return guard already confirmed `row_lock = false`, `r = false` and the function returns `Ok(false)` — `row_type` is never consulted. On SQLite, `EXISTS` returns `BIGINT`, so `try_get::<bool, _>(0)` fails and the `Err` branch correctly reads `row_type`. The bug is PostgreSQL-specific.

The caller in `rollback_block` (lines 33–37) pushes the script id into `script_id_list_to_remove` when `script_exists_in_output` returns `false`, then deletes all collected ids from the `script` table (line 39). For any type-only script on PostgreSQL, this deletion always fires during a reorg, regardless of whether live outputs still reference the script.

## Impact Explanation
This is a concrete corruption of the rich indexer's persistent state on any PostgreSQL-backed deployment. Script records are deleted while live outputs still reference them. Subsequent `get_cells` or `get_transactions` RPC calls for cells using those type scripts return incorrect or empty results. This matches **Medium — Suboptimal implementation of CKB state storage mechanism** (2001–10000 points): the indexer's state storage is permanently corrupted for the affected scripts after any reorg, and the corruption is not self-healing.

## Likelihood Explanation
No attacker capability is required. Any transaction whose output carries a type script that is not simultaneously used as a lock script (the standard pattern for UDT/NFT/DAO cells) will trigger this path during a reorg. Reorgs are a normal part of chain operation. The condition is met by ordinary user transactions on any PostgreSQL-backed rich indexer node.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // was: row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

## Proof of Concept
1. Deploy a PostgreSQL-backed CKB rich indexer node.
2. Submit a transaction whose output carries a type script `S` with a different lock script.
3. Mine the block containing that transaction.
4. Trigger a reorg removing that block (e.g., via a longer competing chain).
5. During removal, `script_exists_in_output` is called for script `S`: `row_lock` query returns `false`, early-return guard is skipped, `row_type` query returns `true`, but the final `match` reads `row_lock` again → returns `Ok(false)`.
6. The caller deletes script `S` from the script table.
7. `get_cells`/`get_transactions` RPC calls for cells using script `S` return empty or corrupt results.

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
