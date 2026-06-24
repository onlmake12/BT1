The bug at line 252 is confirmed in the actual code. Let me verify the exact behavior:

Audit Report

## Title
Copy-Paste Bug in `script_exists_in_output` Causes Permanent Script Table Corruption on PostgreSQL During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary

In `script_exists_in_output`, the second `match` block at line 252 reads from `row_lock` instead of `row_type`. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds and always returns `false` at that point (the lock-script check already returned false to reach line 252), so the type-script existence check is silently skipped. During any chain reorg, `rollback_block` uses this function to decide which scripts to garbage-collect; on PostgreSQL, every `type_script_id` from the rolled-back block is unconditionally deleted from the `script` table, even if it is still referenced by surviving outputs, permanently corrupting the rich indexer's state.

## Finding Description

`script_exists_in_output` executes two `SELECT EXISTS` queries:

- `row_lock` — checks `WHERE lock_script_id = $1` (lines 208–220)
- `row_type` — checks `WHERE type_script_id = $1` (lines 237–249)

The first match block (lines 223–235) is correct: it early-returns `Ok(true)` if the lock-script check is true. Execution only continues past line 235 when the lock-script check is `false`, meaning `row_lock` holds `false`.

The second match block at line 252 is the bug:

```rust
// line 252 — BUG: row_lock used instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),                              // always Ok(false) on PostgreSQL
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1), // correct, but unreachable on PG
}
```

On **PostgreSQL**, `EXISTS` returns `BOOLEAN`, so `row_lock.try_get::<bool, _>(0)` succeeds. Since `row_lock` is already `false` at this point, the function returns `Ok(false)` unconditionally — `row_type` is never read.

On **SQLite**, `EXISTS` returns `BIGINT`, so `try_get::<bool, _>(0)` fails, the `Err(_)` branch runs `row_type.get::<i64, _>(0) == 1`, which is correct. SQLite is unaffected.

The caller in `rollback_block` (lines 33–37) pushes every `type_script_id` for which the function returns `false` into `script_id_list_to_remove`, which is then bulk-deleted from the `script` table (line 39). On PostgreSQL, this means every type script from the rolled-back block is deleted regardless of whether it is still referenced by surviving outputs.

The existing test suite in `rollback.rs` uses only `connect_sqlite`, so this PostgreSQL-specific path has never been exercised by tests.

## Impact Explanation

This is a correctness bug in the CKB rich-indexer state storage mechanism. After any reorg on a PostgreSQL-backed node, scripts still referenced as `type_script_id` in surviving outputs are deleted from the `script` table. All subsequent RPC calls that join against `script` for type-script lookups (`get_cells`, `get_transactions`, `get_cells_capacity` with `script_type = "type"`) silently return empty or incomplete results. The corruption is permanent; there is no self-healing path short of a full indexer rebuild.

This matches the allowed bounty impact: **Medium (2001–10000 points) — Suboptimal implementation of CKB state storage mechanism**, as the rich indexer is the CKB state storage layer and this is an incorrect implementation of its rollback logic.

## Likelihood Explanation

- Chain reorgs are a normal, unprivileged network event triggered by any peer relaying a valid competing chain with more cumulative work.
- PostgreSQL is a documented, officially supported backend for the rich indexer.
- No special attacker capability is required; the bug fires on every reorg on every PostgreSQL deployment.
- The bug is deterministic and repeatable.

## Recommendation

Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Additionally, add a PostgreSQL-backed rollback test that verifies the `script` table count after rolling back a block whose type scripts are still referenced by surviving outputs.

## Proof of Concept

1. Start a rich-indexer node backed by PostgreSQL.
2. Index two blocks: block A contains output O1 with `type_script_id = S` and `lock_script_id = L`; block B (built on A) contains output O2 also with `type_script_id = S`.
3. Trigger a reorg that rolls back block B but keeps block A (so `S` is still referenced by O1).
4. Query: `SELECT id FROM script WHERE id = <S>`.
5. **Expected**: row exists (S is still referenced by O1 in block A).
6. **Actual on PostgreSQL**: row is absent — `script_exists_in_output` returned `Ok(false)` for S because `row_lock` (already `false`) was re-read at line 252 instead of `row_type`. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L33-37)
```rust
        if let Some(type_script_id) = type_script_id
            && !script_exists_in_output(type_script_id, tx).await?
        {
            script_id_list_to_remove.push(type_script_id);
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
