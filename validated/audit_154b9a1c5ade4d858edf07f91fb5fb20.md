Looking at the actual code in `script_exists_in_output` (lines 204–257), the bug described in the question is **real and present in the code**.

The bug at line 252 is real and unambiguous. Here is the full analysis:

---

### Title
`script_exists_in_output` Re-reads `row_lock` Instead of `row_type` on PostgreSQL, Causing Incorrect Type-Script Deletion During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary

`script_exists_in_output` contains a copy-paste error on its final `match` branch: it evaluates `row_lock.try_get::<bool, _>(0)` a second time instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL (where `try_get::<bool, _>` succeeds), this means the type-script existence check always returns the already-known-false lock-script result, so every type-script is unconditionally deleted from the `script` table during any rollback, even when it is still referenced by surviving outputs in other blocks.

### Finding Description

In `script_exists_in_output` (lines 204–257):

```
// First query: does script_id appear as lock_script_id?
let row_lock = sqlx::query("SELECT EXISTS (SELECT 1 FROM output WHERE lock_script_id = $1)")
    .bind(script_id).fetch_one(...).await?;

// Early-return if it IS a lock_script
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => { if r { return Ok(true); } }   // returns true and exits
    Err(_) => { if row_lock.get::<i64, _>(0) == 1 { return Ok(true); } }
}

// Second query: does script_id appear as type_script_id?
let row_type = sqlx::query("SELECT EXISTS (SELECT 1 FROM output WHERE type_script_id = $1)")
    .bind(script_id).fetch_one(...).await?;

// BUG: reads row_lock again, not row_type
match row_lock.try_get::<bool, _>(0) {   // <-- line 252: should be row_type
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [1](#0-0) 

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (returns `Ok`). The function only reaches line 252 when `row_lock` already returned `false` (otherwise the early-return at line 224–227 would have fired). Therefore `Ok(r)` at line 253 always resolves to `Ok(false)`, regardless of what `row_type` contains. [2](#0-1) 

On SQLite, `try_get::<bool, _>` fails with `Err`, so the `Err(_)` branch at line 255 correctly reads `row_type`. All existing tests use SQLite (`connect_sqlite(MEMORY_DB)`), so this PostgreSQL-specific path is never exercised. [3](#0-2) 

### Impact Explanation

`rollback_block` first removes all `output` rows for the rolled-back block, then calls `script_exists_in_output` for every script referenced by those outputs to decide whether to delete the script row. [4](#0-3) 

On PostgreSQL, the type-script check always returns `false`, so every type-script from the rolled-back block is deleted from the `script` table — even when the same type-script is still referenced by live outputs in earlier blocks. This permanently corrupts the indexer's `script`-to-`output` mapping. All subsequent RPC queries (e.g. `get_cells`, `get_transactions`) that filter by those type-scripts will return empty results, silently hiding live cells from callers.

### Likelihood Explanation

The trigger is any chain reorganization — including natural 1-block reorgs that occur in normal operation without any attacker. The only deployment requirement is using the rich-indexer with a PostgreSQL backend, which is a supported and documented configuration. No special privileges, hashpower, or attacker action is required beyond what causes an ordinary reorg.

The `rollback` call path is straightforward: `IndexerSync::rollback` → `AsyncRichIndexer::rollback` → `rollback_block` → `script_exists_in_output`. [5](#0-4) [6](#0-5) 

### Recommendation

Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Add a PostgreSQL integration test for rollback with shared type-scripts across blocks to prevent regression.

### Proof of Concept

1. Start a CKB node with the rich-indexer backed by PostgreSQL.
2. Append **block A** containing two outputs that share the same `type_script T` but have different lock-scripts. Also append **block B** (a different fork) that contains one output also referencing `type_script T`.
3. Trigger a rollback of block B (reorg back to block A).
4. Query the `script` table: `type_script T`'s row is gone even though block A's outputs still reference it.
5. Issue an RPC `get_cells` filtered by `type_script T`: returns 0 results despite live cells existing. [7](#0-6)

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

**File:** util/rich-indexer/src/tests/rollback.rs (L7-8)
```rust
    let storage = connect_sqlite(MEMORY_DB).await;
    let indexer = AsyncRichIndexer::new(
```

**File:** util/rich-indexer/src/indexer/mod.rs (L100-112)
```rust
    /// Rollback the indexer to a previous state
    fn rollback(&self) -> Result<(), Error> {
        let future = self.async_rich_indexer.rollback();
        let rollback_ret = self.async_runtime.block_on(future);

        if rollback_ret.is_ok()
            && let Some((number, _)) = self.tip().ok().flatten()
        {
            set_indexer_tip_metric(number);
        }

        rollback_ret
    }
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
