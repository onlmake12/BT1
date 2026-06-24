The code is confirmed. Line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. [1](#0-0) 

The full logic path is verified:

1. `row_lock` is fetched at lines 208–220 and evaluated at lines 222–235. If true, the function returns early. [2](#0-1) 
2. `row_type` is fetched at lines 237–249 with the correct `WHERE type_script_id = $1` query. [3](#0-2) 
3. At line 252, `row_lock` is evaluated again instead of `row_type`. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds and returns `false` (since the early-return guard already confirmed it was false). `row_type` is never read. On SQLite, `try_get::<bool, _>` fails, so the `Err` arm correctly falls through to `row_type.get::<i64, _>(0) == 1`. [4](#0-3) 
4. The caller `rollback_block` deletes outputs first, then calls `script_exists_in_output` to decide which scripts to purge. [5](#0-4) 

---

Audit Report

## Title
Copy-paste bug causes `script_exists_in_output` to re-evaluate `row_lock` instead of `row_type` on PostgreSQL, silently deleting type-script-only scripts during rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
At line 252 of `script_exists_in_output`, the match expression evaluates `row_lock` a second time instead of the freshly-fetched `row_type`. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds and always returns `false` at this point (the early-return guard already confirmed it), so the function unconditionally returns `false` for any script that is referenced only as a `type_script_id`. During `rollback_block`, this causes every such script to be deleted from the `script` table, corrupting the rich-indexer's state for all subsequent RPC queries. SQLite is unaffected because `try_get::<bool, _>` fails there, falling through to the correct `row_type` read.

## Finding Description
`script_exists_in_output` performs two sequential `EXISTS` queries:

- **`row_lock`** (lines 208–220): `WHERE lock_script_id = $1`
- **`row_type`** (lines 237–249): `WHERE type_script_id = $1`

After the early-return guard at lines 222–235 confirms `row_lock` is `false`, the function fetches `row_type`. The final match at line 252 should evaluate `row_type`, but instead re-evaluates `row_lock`:

```rust
// line 252 — BUG: row_lock used instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),                              // always Ok(false) on PG
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1), // never reached on PG
}
```

On PostgreSQL, `EXISTS` returns `BOOLEAN`, so `try_get::<bool, _>(0)` succeeds. Since `row_lock` was already confirmed false, this returns `Ok(false)`. `row_type` is never consulted. The function returns `false` regardless of whether the script is referenced by thousands of surviving outputs as a `type_script_id`.

`rollback_block` removes outputs first (line 25), then iterates the collected `(id, lock_script_id, type_script_id)` tuples and calls `script_exists_in_output` to decide which scripts are safe to delete (lines 29–38). Because the function always returns `false` for the type-script branch on PostgreSQL, every script used exclusively as a `type_script` is pushed into `script_id_list_to_remove` and deleted (line 39).

## Impact Explanation
This is a concrete incorrect implementation of the CKB state storage mechanism (rich-indexer). Any script that is exclusively used as a `type_script` — including widely-deployed system scripts such as the Nervos DAO script, xUDT, SUDT, and Spore — is permanently deleted from the `script` table on the first rollback event on a PostgreSQL-backed node. All subsequent `get_cells` / `get_cells_capacity` RPC calls that filter by such a script hash return zero results. The indexer's state is silently corrupted with no error, no log warning, and no self-healing path short of a full re-sync from genesis. This matches **Medium (2001–10000 points): Suboptimal/incorrect implementation of CKB state storage mechanism**.

## Likelihood Explanation
No attacker is required. A single natural 1-block reorg — a routine, zero-cost network event — is sufficient to trigger the deletion. The only precondition is that the rolled-back block contains at least one output whose `type_script` is also referenced by outputs in surviving blocks, which is true for any block touching a widely-used system script. The bug is PostgreSQL-specific and silent, making it easy to miss in production.

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
1. Start a PostgreSQL-backed CKB rich-indexer node.
2. Index several blocks containing outputs whose `type_script` is the Nervos DAO system script (or any script used exclusively as a `type_script`).
3. Trigger `rollback_block` (e.g., via a natural 1-block reorg or by calling the rollback path directly in a test).
4. Query `SELECT COUNT(*) FROM script WHERE id = <dao_script_id>` directly on the PostgreSQL database.
5. **Expected:** count = 1 (script row survives).
6. **Actual:** count = 0 (script row deleted).
7. Confirm via `get_cells` RPC with `script_type = "type"` and the DAO script hash: returns zero results after the rollback.

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
