The bug at line 252 is confirmed in the actual code. Let me verify the full context before rendering a verdict.

Audit Report

## Title
`script_exists_in_output` reads stale `row_lock` instead of `row_type` on PostgreSQL, causing incorrect type-script deletion and indexer stall during reorg — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary

In `script_exists_in_output`, after fetching `row_type` (the `type_script_id` EXISTS query at lines 237–249), the final `match` at line 252 mistakenly calls `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this always returns `Ok(false)` for the type-script check, causing `rollback_block` to unconditionally queue every type-script ID for deletion even when surviving outputs still reference it. On PostgreSQL with FK constraints enforced, the subsequent `DELETE FROM script WHERE id IN (...)` raises a foreign-key violation, the DB transaction aborts, and the indexer stalls permanently.

## Finding Description

`script_exists_in_output` performs two sequential EXISTS queries: one against `lock_script_id` (stored in `row_lock`, lines 208–220) and one against `type_script_id` (stored in `row_type`, lines 237–249). After the first query, lines 222–235 early-return `Ok(true)` if the script is still a lock script. If execution reaches line 252, `row_lock` is already known to hold `false`. The copy-paste error at line 252 re-evaluates `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, `try_get::<bool, _>` succeeds on `row_lock`, returning `Ok(false)`, so the function returns `Ok(false)` regardless of `row_type`'s actual value. The `row_type` variable is only consumed in the `Err(_)` branch (line 255), which is the SQLite code path (SQLite returns BIGINT, not BOOLEAN, causing `try_get::<bool, _>` to fail). All existing rollback tests use the in-memory SQLite backend and therefore do not exercise this path.

In `rollback_block` (lines 28–39), the result of `script_exists_in_output` gates whether a `type_script_id` is pushed into `script_id_list_to_remove`. Because the function always returns `false` on PostgreSQL for the type-script branch, every type-script ID from the rolled-back block is unconditionally added to the deletion list and then deleted via `remove_batch_by_blobs("script", "id", ...)` at line 39, even when other surviving outputs still hold a foreign-key reference to that `script.id`.

## Impact Explanation

On PostgreSQL with FK constraints (the default schema), `remove_batch_by_blobs` issues `DELETE FROM script WHERE id IN (...)`. PostgreSQL raises a FK violation because surviving `output` rows still reference those `script.id` values. The error propagates as `Error::DB(...)` out of `rollback_block` → `AsyncRichIndexer::rollback` → `RichIndexer::rollback`. The DB transaction is aborted, the rollback never completes, and the rich-indexer's RPC endpoints (`get_cells`, `get_transactions`, `get_cells_capacity`, `get_indexer_tip`) become permanently unavailable until the process is manually restarted. This matches the allowed impact: **Note (0–500 points) — local RPC API crash**.

## Likelihood Explanation

No attacker is required. Reorgs are a normal, frequent occurrence on CKB mainnet. Type scripts (UDTs, NFTs, DAO) are ubiquitous and routinely shared across many blocks. PostgreSQL is a first-class supported backend documented in `util/rich-indexer/README.md`. The condition "a type script from the rolled-back block is also referenced by a surviving block's output" is trivially satisfied by any token transfer or DAO operation spanning multiple blocks. The bug is deterministically triggered on every reorg involving a shared type script on PostgreSQL.

## Recommendation

Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// util/rich-indexer/src/indexer/remove.rs, line 252
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Additionally, add a rollback test using a PostgreSQL backend (or a multi-block SQLite scenario where a type script is shared across blocks) to cover this code path.

## Proof of Concept

1. Start the rich-indexer with a PostgreSQL backend (`db_type = "postgres"` in `ckb.toml`).
2. Append block N-1 containing output O0 whose `type_script` is script S.
3. Append block N containing output O1 whose `type_script` is the same script S (same `code_hash`, `hash_type`, `args` → same `script.id`).
4. Trigger a reorg that rolls back block N (call `RichIndexer::rollback()`).
5. Observe: `script_exists_in_output(S.id)` returns `false` (bug — `row_lock` is re-read instead of `row_type`), S is pushed into `script_id_list_to_remove`.
6. `remove_batch_by_blobs("script", "id", [S.id], tx)` issues `DELETE FROM script WHERE id = S.id`.
7. PostgreSQL raises FK violation because O0 (in block N-1) still references `S.id` via `type_script_id`.
8. `rollback_block` returns `Error::DB("foreign key violation")`, the indexer stalls.
9. Verify: `SELECT COUNT(*) FROM script WHERE id = <S.id>` should equal 1 after rollback; it equals 0 (or the DELETE fails with FK error), proving incorrect behavior. [1](#0-0) [2](#0-1) [3](#0-2)

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
