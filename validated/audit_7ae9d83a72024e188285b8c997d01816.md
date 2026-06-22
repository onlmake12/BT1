The bug at line 252 of `remove.rs` is real and confirmed. Here is the analysis:

---

### Title
Indexer Script Table Corruption via Copy-Paste Bug in `script_exists_in_output` During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary

`script_exists_in_output` reads `row_lock` instead of `row_type` when evaluating the result of the `type_script_id` existence query on PostgreSQL. This causes a type script that is still referenced by a surviving output to be incorrectly deleted from the `script` table during any block rollback, corrupting the indexer's deduplication invariant.

### Finding Description

In `script_exists_in_output`, two SQL queries are issued:

1. `row_lock` — checks `WHERE lock_script_id = $1`
2. `row_type` — checks `WHERE type_script_id = $1`

The function returns early if `row_lock` is true. If not, it falls through to evaluate `row_type`. But at line 252, the final `match` reads `row_lock` again instead of `row_type`: [1](#0-0) 

```rust
let row_type = sqlx::query(...WHERE type_script_id = $1...).fetch_one(...).await?;

// BUG: should be row_type.try_get, not row_lock.try_get
match row_lock.try_get::<bool, _>(0) {   // line 252
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On **PostgreSQL**, `try_get::<bool, _>(0)` succeeds on `row_lock`, so `Ok(r)` is returned — where `r` is the result of the **lock** query, not the type query. The `row_type` result is silently discarded.

On **SQLite**, `try_get::<bool, _>(0)` fails (SQLite returns BIGINT), so the `Err(_)` branch correctly reads `row_type.get::<i64, _>(0)`. SQLite is unaffected.

### Impact Explanation

During `rollback_block`, outputs are deleted first, then `script_exists_in_output` is called to decide whether to garbage-collect each script: [2](#0-1) 

Concrete corruption scenario (PostgreSQL only):

1. Block B (not rolled back) has output O2 with `type_script_id = T1`, where T1 is **not** used as any `lock_script_id`.
2. Block A (being rolled back) also has an output with `type_script_id = T1`.
3. After O1 is removed, `script_exists_in_output(T1)` is called:
   - `row_lock` query: `lock_script_id = T1` → `false` (T1 is never a lock script).
   - `row_type` query: `type_script_id = T1` → `true` (O2 still references it).
   - Bug: `row_lock.try_get::<bool, _>(0)` returns `Ok(false)` → function returns `false`.
4. T1 is added to `script_id_list_to_remove` and deleted from the `script` table.
5. O2 now has a dangling `type_script_id` foreign key. All subsequent `get_cells`/`get_transactions` queries that join `output` with `script` on this ID return wrong or missing results.

### Likelihood Explanation

Reorgs are a normal, non-adversarial event in CKB. Any reorg where:
- A rolled-back block and a surviving block share a type script, **and**
- That type script is not simultaneously used as a lock script in any surviving output

…will trigger the corruption on PostgreSQL-backed indexer deployments. No attacker action is required; natural chain reorganization is sufficient.

### Recommendation

Fix line 252 to read `row_type` instead of `row_lock`:

```rust
// Before (buggy):
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}

// After (correct):
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

### Proof of Concept

1. Start a CKB node with a PostgreSQL-backed rich indexer.
2. Insert Block A containing a transaction with output O1: `lock_script_id = L1`, `type_script_id = T1`.
3. Insert Block B (on a different branch) containing output O2: `lock_script_id = L2`, `type_script_id = T1` (same type script, T1 is not used as any lock script).
4. Trigger a reorg that rolls back Block A.
5. Query the `script` table: T1 is absent.
6. Query `get_cells` filtering by T1's script hash: returns empty, even though O2 is live. [3](#0-2)

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L25-38)
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
