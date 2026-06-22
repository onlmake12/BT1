The bug is real and clearly present in the code. Let me confirm the exact details.

At line 252 of `util/rich-indexer/src/indexer/remove.rs`, the second `match` in `script_exists_in_output` reads `row_lock` instead of `row_type`: [1](#0-0) 

The function queries two separate rows:
- `row_lock` (lines 208–220): checks if `script_id` appears as a `lock_script_id` in any surviving output
- `row_type` (lines 237–249): checks if `script_id` appears as a `type_script_id` in any surviving output

The first `match` at line 223 correctly uses `row_lock` and returns early with `Ok(true)` if the lock query is positive. [2](#0-1) 

But the second `match` at line 252 re-reads `row_lock` instead of `row_type`. [3](#0-2) 

**Effect by backend:**

- **PostgreSQL**: `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN`). Since execution only reaches line 252 when `row_lock` was `false` (otherwise the early return at line 226 would have fired), this branch always returns `Ok(false)` — ignoring `row_type` entirely. Any script used *only* as a type script (not a lock script) in surviving outputs will be incorrectly reported as absent and deleted.
- **SQLite**: `row_lock.try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err(_)` arm runs and correctly reads `row_type.get::<i64, _>(0) == 1`. SQLite is unaffected.

---

### Title
Copy-paste bug in `script_exists_in_output` causes type scripts to be incorrectly deleted from the script table during block rollback on PostgreSQL — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary
During a block rollback in the rich indexer, `script_exists_in_output` is supposed to check whether a script ID is still referenced by any surviving output (either as a lock or type script) before deleting it from the `script` table. Due to a copy-paste error at line 252, the type-script existence check re-reads `row_lock` instead of `row_type`. On PostgreSQL this always evaluates to `false`, causing type scripts that are still referenced by surviving outputs to be unconditionally deleted.

### Finding Description
`script_exists_in_output` performs two SQL `EXISTS` queries:

1. `WHERE lock_script_id = $1` → result in `row_lock`
2. `WHERE type_script_id = $1` → result in `row_type`

The final return at line 252 should evaluate `row_type`, but instead evaluates `row_lock` a second time:

```rust
// line 252 — BUG: should be row_type.try_get::<bool, _>(0)
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),                              // always false on PG
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1), // correct on SQLite
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds and always returns `false` at this point (the `true` case was already handled by the early return). The `row_type` result is never consulted. [4](#0-3) 

### Impact Explanation
After rollback, the `script` table is missing rows for type scripts that are still referenced by surviving outputs. Subsequent `get_cells` RPC calls join on `script.id`; with the row gone, those cells are invisible in query results. Applications relying on the rich indexer for cell liveness (e.g., wallets, dApps) will incorrectly conclude that cells whose type scripts were deleted do not exist, leading to incorrect cell liveness reporting. The database corruption is permanent until the indexer is rebuilt.

### Likelihood Explanation
Any reorg (including short 1-block reorgs, which occur naturally and can be induced by an attacker mining a competing tip) on a PostgreSQL-backed rich indexer node triggers this path. No special privileges are required. The trigger is the standard `rollback_block` path called during chain reorganization. [5](#0-4) 

### Recommendation
Change line 252 from:
```rust
match row_lock.try_get::<bool, _>(0) {
```
to:
```rust
match row_type.try_get::<bool, _>(0) {
```

### Proof of Concept
1. Configure a CKB rich indexer with a PostgreSQL backend.
2. Construct a block containing one transaction with two outputs that share the same type script (same `type_script_id` in the `output` table), where neither output uses that script as a lock script.
3. Append the block; verify the type script row exists in the `script` table.
4. Trigger a rollback (e.g., by appending a competing block at the same height).
5. After rollback, query `SELECT * FROM script WHERE id = <type_script_id>` — the row is absent despite no surviving output referencing it as a lock script. If any other surviving output references the same type script, it will also be missing, confirming the invariant violation.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L7-51)
```rust
pub(crate) async fn rollback_block(tx: &mut Transaction<'_, Any>) -> Result<(), Error> {
    let block_id = if let Some(block_id) = query_tip_id(tx).await? {
        block_id
    } else {
        return Ok(());
    };

    let tx_id_list = query_tx_id_list_by_block_id(block_id, tx).await?;
    let output_lock_type_list = query_outputs_by_tx_id_list(&tx_id_list, tx).await?;

    // update spent cells
    reset_spent_cells(&tx_id_list, tx).await?;

    // remove transactions, associations, inputs, output
    remove_batch_by_blobs("ckb_transaction", "id", &tx_id_list, tx).await?;
    remove_batch_by_blobs("tx_association_cell_dep", "tx_id", &tx_id_list, tx).await?;
    remove_batch_by_blobs("tx_association_header_dep", "tx_id", &tx_id_list, tx).await?;
    remove_batch_by_blobs("input", "consumed_tx_id", &tx_id_list, tx).await?;
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

    // remove block and block associations
    let uncle_id_list = query_uncle_id_list_by_block_id(block_id, tx).await?;
    remove_batch_by_blobs("block", "id", &[block_id], tx).await?;
    remove_batch_by_blobs("block_association_proposal", "block_id", &[block_id], tx).await?;
    remove_batch_by_blobs("block_association_uncle", "block_id", &[block_id], tx).await?;

    // remove uncles
    remove_batch_by_blobs("block", "id", &uncle_id_list, tx).await?;
    remove_batch_by_blobs("block_association_proposal", "block_id", &uncle_id_list, tx).await?;

    Ok(())
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
