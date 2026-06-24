The code at line 252 is confirmed exactly as claimed: [1](#0-0) 

Line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. `row_type` is fetched at lines 237–249 but is never consulted on the PostgreSQL path. [2](#0-1) 

The early-return at lines 223–235 already handled the `row_lock == true` case, so execution only reaches line 252 when `row_lock` holds `false`. On PostgreSQL, `try_get::<bool, _>(0)` succeeds on `row_lock`, returning `Ok(false)` unconditionally. [3](#0-2) 

The caller at lines 33–37 uses this return value to push `type_script_id` into `script_id_list_to_remove`, which is then deleted at line 39. [4](#0-3) 

All claims in the report are verified against the actual code. The bug is real, the code path is exactly as described, and the impact is correctly scoped.

---

Audit Report

## Title
Copy-Paste Bug in `script_exists_in_output` Silently Skips Type-Script Existence Check on PostgreSQL During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this causes the function to unconditionally return `Ok(false)` for any script that is not a lock script, because `row_lock` holds `false` at that point (the `true` case was already handled by the early return). During `rollback_block`, every `type_script_id` from the rolled-back block is pushed into `script_id_list_to_remove` and deleted, even when those scripts are still referenced by surviving outputs, permanently corrupting the rich indexer's `script` table on PostgreSQL.

## Finding Description
`script_exists_in_output` executes two SQL `EXISTS` queries:

- `row_lock` (lines 208–220): `WHERE lock_script_id = $1`
- `row_type` (lines 237–249): `WHERE type_script_id = $1`

The first match block (lines 223–235) is correct: if `row_lock` is `true`, return early with `Ok(true)`; otherwise fall through to the type-script check.

The second match block (lines 251–256) is the bug:

```rust
// pg type is BOOLEAN
match row_lock.try_get::<bool, _>(0) {   // BUG: should be row_type
    Ok(r) => Ok(r),                       // always Ok(false) on PostgreSQL
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1), // correct, but unreachable on PG
}
```

On **PostgreSQL**, `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN`). Execution only reaches line 252 when the lock-script check was `false`, so `row_lock` holds `false`. The function returns `Ok(false)` unconditionally, ignoring `row_type` entirely.

On **SQLite**, `row_lock.try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err` branch executes `row_type.get::<i64, _>(0) == 1`, which is correct. SQLite is unaffected.

The caller in `rollback_block` (lines 33–37) uses the return value to decide whether to delete the script. On PostgreSQL, `script_exists_in_output` always returns `false` for any script that is not a lock script, so every `type_script_id` from the rolled-back block is unconditionally added to `script_id_list_to_remove` and deleted via `remove_batch_by_blobs("script", "id", &script_id_list_to_remove, tx)` at line 39 — even if those scripts are still referenced by outputs in the remaining chain.

## Impact Explanation
This is a correctness bug in the CKB rich indexer's state storage mechanism. After any reorg on a PostgreSQL-backed rich-indexer node, scripts still referenced as `type_script_id` in surviving outputs are permanently deleted from the `script` table. All subsequent RPC calls that join against `script` for type-script lookups (`get_cells`, `get_transactions`, `get_cells_capacity` with `script_type = "type"`) silently return empty or incomplete results. The corruption is permanent until the indexer is fully rebuilt. This matches **Medium (2001–10000 points) — Suboptimal/incorrect implementation of CKB state storage mechanism**.

## Likelihood Explanation
Chain reorgs are a normal, unprivileged network event: any peer relaying a valid competing chain with more cumulative work triggers `rollback_block`. PostgreSQL is a documented, officially supported backend for the rich indexer. No special attacker capability is required; the bug fires on every reorg on every PostgreSQL deployment. The bug is deterministic and repeatable: every reorg that involves outputs with type scripts will trigger it.

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
6. **Actual on PostgreSQL**: row is absent — `script_exists_in_output` returned `Ok(false)` because `row_lock` (holding `false`) was re-read at line 252 instead of `row_type`.

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

**File:** util/rich-indexer/src/indexer/remove.rs (L237-249)
```rust
    let row_type = sqlx::query(
        r#"
        SELECT EXISTS (
            SELECT 1
            FROM output
            WHERE type_script_id = $1
        )
        "#,
    )
    .bind(script_id)
    .fetch_one(tx.as_mut())
    .await
    .map_err(|err| Error::DB(err.to_string()))?;
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
