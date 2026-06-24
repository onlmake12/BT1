The code at line 252 is confirmed to contain exactly the bug described. [1](#0-0) 

`row_lock.try_get::<bool, _>(0)` is used at line 252 instead of `row_type.try_get::<bool, _>(0)`. Since the first match block (lines 223–235) only returns early when `row_lock` is `true`, reaching line 252 means `row_lock` is already known to be `false`. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds and returns `Ok(false)` — `row_type` is never consulted. On SQLite, `try_get::<bool, _>` fails, so the `Err` branch correctly reads `row_type`. The bug is PostgreSQL-specific and deterministic. [2](#0-1) 

---

Audit Report

## Title
Wrong Row Variable in `script_exists_in_output` Causes Premature Type-Script Deletion on PostgreSQL During Block Rollback - (File: `util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the final `match` block at line 252 reads `row_lock` (the already-evaluated lock-script existence result) instead of `row_type` (the type-script existence result). On PostgreSQL, this causes the function to always return `false` for any script referenced only as a type script. As a result, `rollback_block` incorrectly adds those type-script IDs to `script_id_list_to_remove` and deletes them from the `script` table, permanently corrupting the rich indexer's state on every block rollback.

## Finding Description
`script_exists_in_output` (lines 204–257) executes two SQL `EXISTS` queries:

1. `row_lock` (lines 208–220): `WHERE lock_script_id = $1`
2. `row_type` (lines 237–249): `WHERE type_script_id = $1`

The first `match` block (lines 223–235) returns `Ok(true)` early if the script is found as a lock script; otherwise it falls through. At that point `row_lock` is definitively `false`. The second `match` block at line 252 is supposed to decode `row_type`, but instead reads `row_lock` again:

```rust
// line 252 — BUG: row_lock instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),                          // always Ok(false) on PostgreSQL
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On **PostgreSQL**, `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN`) and returns `Ok(false)` — `row_type` is never read. On **SQLite**, `try_get::<bool, _>` fails (SQLite returns `BIGINT`), so the `Err` branch correctly reads `row_type`. The bug is PostgreSQL-only.

The caller `rollback_block` (lines 33–37) uses this return value to decide whether to delete a script row:

```rust
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
```

Because `script_exists_in_output` returns `false` for any script referenced only as a type script, every such script is pushed to `script_id_list_to_remove` and deleted at line 39, even when other outputs still reference it.

## Impact Explanation
The `script` table is the authoritative source for script metadata in the rich indexer. Premature deletion of type-script rows breaks the relational integrity of the indexer and causes `get_cells`, `get_transactions`, and `get_cells_capacity` RPC calls that filter by those type scripts to return empty or incorrect results. The corruption is permanent until the indexer is fully rebuilt. This constitutes a **suboptimal (incorrect) implementation of the CKB state storage mechanism**, matching the **Medium (2001–10000 points)** bounty impact tier.

## Likelihood Explanation
Chain reorganizations are a normal, externally triggerable event requiring no special attacker privilege — any peer presenting a heavier competing chain causes a reorg. PostgreSQL is the recommended production database backend for the rich indexer. The bug is deterministic: every reorg that rolls back a block containing outputs with type scripts (virtually every mainnet block) triggers the incorrect deletion on PostgreSQL. No victim mistake or unrealistic assumption is required.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // corrected
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Additionally, add an integration test that rolls back a block containing an output whose type script is not shared with any lock script, and asserts that the script row is **not** deleted when other outputs still reference it as a type script.

## Proof of Concept
1. Start a CKB node with the rich indexer configured to use PostgreSQL.
2. Submit and mine a block containing a transaction whose output has a unique type script (not used as any lock script).
3. Trigger a chain reorganization that rolls back that block (e.g., by submitting a heavier competing fork).
4. `rollback_block` calls `script_exists_in_output(type_script_id, tx)`.
5. The first query (`lock_script_id = $1`) returns `false`; execution falls through to the second query.
6. The second query (`type_script_id = $1`) is stored in `row_type`, but line 252 reads `row_lock` again, returning `Ok(false)`.
7. The type script's row is added to `script_id_list_to_remove` and deleted from the `script` table.
8. Subsequent `get_cells` RPC calls filtering by that type script return empty results, even if the script is re-introduced in a later block.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L222-256)
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

    // pg type is BOOLEAN
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```
