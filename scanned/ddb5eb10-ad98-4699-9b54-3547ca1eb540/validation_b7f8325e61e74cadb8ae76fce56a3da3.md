The bug at line 252 is confirmed exactly as described. `row_lock.try_get::<bool, _>(0)` is used instead of `row_type.try_get::<bool, _>(0)`. [1](#0-0) 

The early-return guard at lines 222–235 already confirmed `row_lock` is `false` before reaching line 252. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds and returns `Ok(false)` again — `row_type` is never read. On SQLite, `try_get::<bool, _>` fails, falling through to the `Err` arm which correctly reads `row_type`. [2](#0-1) 

The caller `rollback_block` deletes outputs first, then calls `script_exists_in_output` to decide whether to delete orphaned scripts. [3](#0-2) 

---

Audit Report

## Title
Copy-paste bug in `script_exists_in_output` re-reads `row_lock` instead of `row_type` on PostgreSQL, silently deleting type-script-only scripts during rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
At line 252 of `script_exists_in_output`, `row_lock.try_get::<bool, _>(0)` is evaluated a second time instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this always returns `false` for the type-script existence check because `row_lock` was already confirmed false by the early-return guard. As a result, any script referenced exclusively as a `type_script_id` — never as a `lock_script_id` — is unconditionally deleted from the `script` table during every `rollback_block` call on a PostgreSQL-backed rich-indexer node.

## Finding Description
`script_exists_in_output` (lines 204–257) performs two sequential `EXISTS` queries: one for `lock_script_id` (`row_lock`, lines 208–220) and one for `type_script_id` (`row_type`, lines 237–249). After the first query, lines 222–235 return `Ok(true)` early if `row_lock` is true. If execution reaches line 252, `row_lock` is definitionally false. The bug is that line 252 reads `row_lock` again:

```rust
// line 252 — BUG: should be row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),          // PostgreSQL: succeeds, returns Ok(false)
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),  // SQLite: correct
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds (PG `EXISTS` returns `BOOLEAN`), yields `Ok(false)`, and `row_type` is never consulted. On SQLite, `try_get::<bool, _>` fails (SQLite returns `BIGINT`), so the `Err` arm correctly reads `row_type`. The caller `rollback_block` removes outputs before calling this function (line 25), then uses the return value to decide whether to delete the script row (lines 30–38). Because the function always returns `false` for the type-script branch on PostgreSQL, every script used only as a `type_script` is pushed into `script_id_list_to_remove` and deleted.

## Impact Explanation
This is an incorrect implementation of the CKB state storage mechanism (rich-indexer). After any reorg on a PostgreSQL-backed node, scripts used exclusively as `type_script` (e.g., the Nervos DAO system script, xUDT, SUDT, Spore) are permanently deleted from the `script` table. All subsequent `get_cells`/`get_cells_capacity` RPC queries filtering by those type script hashes return zero results. The indexer's stored state diverges silently from the actual chain state with no error or warning. Recovery requires a full re-sync from genesis. This matches: **Medium (2001–10000 points) — Suboptimal/incorrect implementation of CKB state storage mechanism.**

## Likelihood Explanation
The only precondition is a reorg (a normal, zero-attacker blockchain event) on a PostgreSQL-backed rich-indexer node where the rolled-back block contains at least one output whose `type_script` is also referenced by surviving outputs. This is trivially satisfied for any widely-used system script. No attacker capability is required; a single natural 1-block reorg is sufficient. The bug is silent: no error is returned, no log is emitted, and the node continues operating with a corrupted index.

## Recommendation
Change line 252 from `row_lock` to `row_type`:

```rust
// BEFORE (buggy)
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}

// AFTER (correct)
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Additionally, deduplicate `script_id_list_to_remove` before passing it to `remove_batch_by_blobs` to avoid double-deletion when the same script appears in multiple outputs of the same rolled-back block.

## Proof of Concept
1. Start a PostgreSQL-backed CKB rich-indexer node and sync several blocks containing DAO deposit cells (outputs with the DAO system script as `type_script`, never as `lock_script`).
2. Index one additional block containing a DAO-related transaction (also uses the DAO system script as `type_script`).
3. Trigger `rollback_block` via a natural or forced 1-block reorg removing that last block.
4. Query `get_cells` with `script = <DAO type script hash>`, `script_type = "type"`.
5. **Expected:** all DAO deposit cells from surviving blocks are returned.
6. **Actual:** zero results — the DAO system script row has been deleted from the `script` table because `script_exists_in_output` returned `false` for the type-script check on PostgreSQL.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L25-39)
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
