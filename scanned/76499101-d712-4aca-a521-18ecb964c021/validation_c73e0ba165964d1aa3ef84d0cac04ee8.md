The bug at line 252 is real and exactly as described. Let me confirm the full code path:

The code is confirmed. Line 252 unambiguously reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. The `row_type` variable is fetched at lines 237–249 but never actually read on the PostgreSQL path — `row_lock` (already holding `false` at that point) is re-read instead.

---

Audit Report

## Title
Copy-Paste Bug in `script_exists_in_output` Silently Skips Type-Script Existence Check on PostgreSQL During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the final `match` block at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, `try_get::<bool, _>(0)` succeeds on `row_lock` (which holds `false` at that point, since the early-return at line 225–227 already handled the `true` case), so the function unconditionally returns `Ok(false)` without ever consulting `row_type`. During `rollback_block`, this causes every `type_script_id` from the rolled-back block to be pushed into `script_id_list_to_remove` and deleted from the `script` table, even when those scripts are still referenced by surviving outputs, permanently corrupting the rich indexer's `script` table on PostgreSQL.

## Finding Description

`script_exists_in_output` executes two SQL `EXISTS` queries:

- `row_lock` (lines 208–220): `WHERE lock_script_id = $1`
- `row_type` (lines 237–249): `WHERE type_script_id = $1`

The first match block (lines 223–235) is correct: if `row_lock` is `true`, return early with `Ok(true)`; otherwise fall through to the type-script check.

The second match block (lines 252–256) is the bug:

```rust
// line 252 — BUG: row_lock used instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),                              // always Ok(false) on PostgreSQL
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1), // correct, but unreachable on PG
}
```

On **PostgreSQL**, `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN`). Execution only reaches line 252 when the lock-script check was `false`, so `row_lock` holds `false`. The function returns `Ok(false)` unconditionally, ignoring `row_type` entirely.

On **SQLite**, `row_lock.try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err` branch executes `row_type.get::<i64, _>(0) == 1`, which is correct. SQLite is unaffected.

The caller in `rollback_block` (lines 33–37) uses the return value to decide whether to delete the script:

```rust
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
```

On PostgreSQL, `script_exists_in_output` always returns `false` for any script that is not a lock script, so every `type_script_id` from the rolled-back block is unconditionally added to `script_id_list_to_remove` and deleted via `remove_batch_by_blobs("script", "id", &script_id_list_to_remove, tx)` at line 39 — even if those scripts are still referenced by outputs in the remaining chain.

## Impact Explanation

This is a correctness bug in the CKB rich indexer's state storage mechanism. After any reorg on a PostgreSQL-backed rich-indexer node, scripts still referenced as `type_script_id` in surviving outputs are permanently deleted from the `script` table. All subsequent RPC calls that join against `script` for type-script lookups (`get_cells`, `get_transactions`, `get_cells_capacity` with `script_type = "type"`) silently return empty or incomplete results. The corruption is permanent until the indexer is fully rebuilt. This matches the allowed impact: **Medium (2001–10000 points) — Suboptimal/incorrect implementation of CKB state storage mechanism**, as the rich indexer is the documented CKB state indexing layer and this bug causes irreversible data loss within it.

## Likelihood Explanation

- Chain reorgs are a normal, unprivileged network event: any peer relaying a valid competing chain with more cumulative work triggers `rollback_block`.
- PostgreSQL is a documented, officially supported backend for the rich indexer.
- No special attacker capability is required; the bug fires on every reorg on every PostgreSQL deployment.
- The bug is deterministic and repeatable: every reorg that involves outputs with type scripts will trigger it.

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

## Proof of Concept

1. Run a PostgreSQL-backed rich-indexer node.
2. Index a block B1 containing output O1 with `type_script_id = S` and `lock_script_id = L` (where `S ≠ L`). Ensure another surviving output O2 (in a prior block) also has `type_script_id = S`.
3. Trigger a reorg that rolls back B1 (O2 remains in the chain, so `S` is still referenced).
4. After rollback, query: `SELECT id FROM script WHERE id = <S>`.
5. **Expected**: row exists (`S` is still referenced by O2).
6. **Actual on PostgreSQL**: row is absent — `script_exists_in_output` returned `Ok(false)` because `row_lock` (holding `false`) was re-read at line 252 instead of `row_type`. [1](#0-0) [2](#0-1) [3](#0-2)

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
