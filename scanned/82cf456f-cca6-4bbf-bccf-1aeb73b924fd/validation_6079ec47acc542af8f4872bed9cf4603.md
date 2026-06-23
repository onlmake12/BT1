### Title
Incorrect Row Variable Used in `script_exists_in_output` Causes Premature Script Deletion on PostgreSQL During Block Rollback - (File: `util/rich-indexer/src/indexer/remove.rs`)

### Summary
In `script_exists_in_output`, the second `match` block reads from `row_lock` (the result of the lock-script existence query) instead of `row_type` (the result of the type-script existence query). On PostgreSQL this silently returns `false` for any script that is referenced only as a type script, causing `rollback_block` to delete those script rows from the database even though they are still live. This corrupts the rich-indexer's relational state and produces wrong answers for every subsequent RPC query that filters by type script.

### Finding Description
`script_exists_in_output` performs two SQL `EXISTS` queries:

1. `row_lock` — checks `WHERE lock_script_id = $1`
2. `row_type` — checks `WHERE type_script_id = $1`

After the first query, if the script is not found as a lock script, execution falls through to the second query. The second `match` block is then supposed to decode `row_type`, but it mistakenly decodes `row_lock` again:

```rust
// line 252 — BUG: should be row_type.try_get, not row_lock.try_get
match row_lock.try_get::<bool, _>(0) {   // <-- wrong variable
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On **PostgreSQL**, `row_lock.try_get::<bool, _>(0)` succeeds and returns the already-known `false` value (the script was not found as a lock script), so the function returns `Ok(false)` without ever consulting `row_type`. The correct fix is `row_type.try_get::<bool, _>(0)`.

On **SQLite**, `try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err` branch is taken and `row_type.get::<i64, _>(0)` is read correctly — SQLite is unaffected. [1](#0-0) 

The caller `rollback_block` uses the return value to decide whether to delete a script row:

```rust
if !script_exists_in_output(lock_script_id, tx).await? {
    script_id_list_to_remove.push(lock_script_id);
}
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
``` [2](#0-1) 

Because `script_exists_in_output` incorrectly returns `false` for any script that is only referenced as a type script, every such script is added to `script_id_list_to_remove` and deleted from the `script` table, even though other outputs still reference it.

### Impact Explanation
The `script` table is the authoritative source for script metadata in the rich indexer. Premature deletion of a type-script row breaks foreign-key relationships and causes `get_cells`, `get_transactions`, and `get_cells_capacity` RPC calls that filter by that type script to return empty or incorrect results. The corruption is permanent until the indexer is rebuilt from scratch. Any user or application relying on the rich indexer for type-script queries (e.g., UDT balance queries, DAO queries) receives silently wrong data after a reorg.

### Likelihood Explanation
Chain reorganizations are a normal, externally triggerable event: any peer that presents a heavier competing chain causes a reorg. The rich indexer is a production component deployed alongside full nodes. PostgreSQL is the recommended production database backend. The bug is deterministic — every reorg that rolls back a block containing outputs with type scripts (which is virtually every block on mainnet) will trigger the incorrect deletion on PostgreSQL.

### Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // <-- corrected
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Add a unit/integration test that rolls back a block containing an output whose type script is not shared with any lock script, and asserts that the script row is **not** deleted when other outputs still reference it as a type script.

### Proof of Concept

1. Start a CKB node with the rich indexer configured to use PostgreSQL.
2. Submit and mine a block containing a transaction whose output has a unique type script (not used as any lock script).
3. Trigger a chain reorganization that rolls back that block (e.g., by submitting a heavier fork).
4. `rollback_block` calls `script_exists_in_output(type_script_id, tx)`.
5. The first query (`lock_script_id = $1`) returns `false` (the script is not a lock script).
6. The second query (`type_script_id = $1`) is executed and stored in `row_type`, but the second `match` reads `row_lock` again, returning `Ok(false)`.
7. The type script's row is added to `script_id_list_to_remove` and deleted.
8. Subsequent `get_cells` RPC calls filtering by that type script return an empty result, even if the script is re-introduced in a later block. [3](#0-2)

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L29-38)
```rust
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
```

**File:** util/rich-indexer/src/indexer/remove.rs (L204-257)
```rust
async fn script_exists_in_output(
    script_id: i64,
    tx: &mut Transaction<'_, Any>,
) -> Result<bool, Error> {
    let row_lock = sqlx::query(
        r#"
        SELECT EXISTS (
            SELECT 1
            FROM output
            WHERE lock_script_id = $1
        )
        "#,
    )
    .bind(script_id)
    .fetch_one(tx.as_mut())
    .await
    .map_err(|err| Error::DB(err.to_string()))?;

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
}
```
