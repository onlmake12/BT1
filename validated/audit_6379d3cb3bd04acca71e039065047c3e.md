The bug is real and clearly visible in the code. Let me confirm the exact details.

The bug is confirmed and concrete. Here is the full analysis:

---

### Title
PostgreSQL `script_exists_in_output` Re-evaluates `row_lock` Instead of `row_type`, Corrupting Type-Script Entries on Block Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary

A copy-paste error at line 252 of `remove.rs` causes `script_exists_in_output` to re-evaluate the **lock-script** query result (`row_lock`) instead of the **type-script** query result (`row_type`) when determining whether a type script is still referenced by remaining outputs. On PostgreSQL this silently returns the wrong boolean, causing type-script rows to be unconditionally deleted from the `script` table during every block rollback, even when other live outputs still reference them. SQLite is unaffected because its integer return type forces the correct `Err(_)` branch.

### Finding Description

`script_exists_in_output` performs two sequential SQL `EXISTS` queries:

1. **Lines 208–220** — `row_lock`: checks `WHERE lock_script_id = $1`
2. **Lines 237–249** — `row_type`: checks `WHERE type_script_id = $1`

The first `match` block (lines 223–235) short-circuits with `Ok(true)` if the script is still a lock script. If not, execution falls through to fetch `row_type`. The second `match` block at **line 252** is supposed to evaluate `row_type`, but instead reads:

```rust
// util/rich-indexer/src/indexer/remove.rs, line 252
match row_lock.try_get::<bool, _>(0) {   // BUG: should be row_type
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [1](#0-0) 

On **PostgreSQL**, `EXISTS` returns a SQL `BOOLEAN`, so `row_lock.try_get::<bool, _>(0)` succeeds and returns `Ok(false)` (the script is not a lock script — that was already established). The function therefore returns `Ok(false)` regardless of what `row_type` contains, meaning the type script is always reported as absent.

On **SQLite**, `EXISTS` returns a `BIGINT`, so `try_get::<bool, _>(0)` fails, the `Err(_)` branch is taken, and `row_type.get::<i64, _>(0) == 1` is correctly evaluated.

The caller in `rollback_block` unconditionally queues every script for which `script_exists_in_output` returns `false`:

```rust
// util/rich-indexer/src/indexer/remove.rs, lines 33–37
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
``` [2](#0-1) 

Because `script_exists_in_output` always returns `false` for type scripts on PostgreSQL, every type-script row from the rolled-back block is deleted — even when other surviving outputs still reference it.

### Impact Explanation

After a rollback on a PostgreSQL-backed rich-indexer:

- Any type script that was shared between the rolled-back block and an earlier block is deleted from the `script` table.
- Surviving `output` rows still hold a foreign-key `type_script_id` pointing to the now-deleted script row.
- Subsequent `get_cells` or `get_transactions` queries filtered by that type script silently return zero results (or produce a DB error depending on join semantics), even though the cells are live.
- The indexer state permanently diverges from the canonical chain state and from any SQLite-backed indexer on the same node.

### Likelihood Explanation

Block rollbacks are a routine, attacker-reachable event: any peer can relay a valid PoW block that extends a fork, causing the node's chain to reorganize and the indexer sync loop to call `indexer.rollback()`. [3](#0-2) 

The rollback path is `try_loop_sync` → `indexer.rollback()` → `AsyncRichIndexer::rollback()` → `rollback_block()` → `script_exists_in_output()`. [4](#0-3) 

No privileged access is required. Any natural or adversarially-induced reorg on a node running the PostgreSQL-backed rich-indexer triggers the bug. All existing rollback tests use SQLite in-memory databases and therefore do not catch this defect. [5](#0-4) 

### Recommendation

Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// Correct fix
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Add a PostgreSQL integration test that appends a block containing a cell whose type script is unique to that block, rolls back, and asserts the script row is removed; then appends two blocks sharing a type script, rolls back the second, and asserts the script row is retained.

### Proof of Concept

1. Start a CKB node with `rich_indexer` configured to use PostgreSQL.
2. Append **Block A** containing a cell with a unique `type_script T` (not used as a lock script anywhere).
3. Append **Block B** containing another cell also referencing `type_script T`.
4. Trigger a rollback of Block B (e.g., via a competing fork).
5. Query `SELECT COUNT(*) FROM script WHERE id = <T_id>` — on PostgreSQL the row is gone (incorrect); on SQLite it remains (correct).
6. Issue `get_cells` with a type-script filter for `T` — returns 0 results on PostgreSQL despite Block A's cell being live. [6](#0-5)

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L33-37)
```rust
        if let Some(type_script_id) = type_script_id
            && !script_exists_in_output(type_script_id, tx).await?
        {
            script_id_list_to_remove.push(type_script_id);
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

**File:** util/indexer-sync/src/lib.rs (L163-170)
```rust
                            } else {
                                info!(
                                    "{} rollback {}, {}",
                                    indexer.get_identity(),
                                    tip_number,
                                    tip_hash
                                );
                                indexer.rollback().expect("rollback block should be OK");
```

**File:** util/rich-indexer/src/indexer/mod.rs (L180-189)
```rust
    pub(crate) async fn rollback(&self) -> Result<(), Error> {
        let mut tx = self
            .store
            .transaction()
            .await
            .map_err(|err| Error::DB(err.to_string()))?;

        rollback_block(&mut tx).await?;

        tx.commit().await.map_err(|err| Error::DB(err.to_string()))
```

**File:** util/rich-indexer/src/tests/rollback.rs (L6-8)
```rust
async fn test_rollback_block_0() {
    let storage = connect_sqlite(MEMORY_DB).await;
    let indexer = AsyncRichIndexer::new(
```
