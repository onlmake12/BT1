### Title
Wrong Row Variable in Existence Check Causes Silent Type-Script Deletion During Block Rollback — (`File: util/rich-indexer/src/indexer/remove.rs`)

### Summary

In `util/rich-indexer/src/indexer/remove.rs`, the `script_exists_in_output` function contains a copy-paste error: after querying whether a script is referenced as a **type** script (`row_type`), the final match arm reads from `row_lock` (the **lock** script query result) instead of `row_type`. On PostgreSQL this causes the function to return `false` ("script does not exist") for any script that is referenced **only** as a type script, triggering its permanent deletion from the `script` table during block rollback.

### Finding Description

`script_exists_in_output` is called by `rollback_block` to decide whether a script record can be safely removed from the indexer database. It performs two SQL `EXISTS` queries: one for `lock_script_id` (`row_lock`) and one for `type_script_id` (`row_type`). [1](#0-0) 

The first half of the function is correct: if `row_lock` is true the function returns early with `Ok(true)`. The second half queries `row_type` but then mistakenly re-reads `row_lock` in the final match:

```rust
// line 237-249: row_type is fetched correctly
let row_type = sqlx::query(
    r#"SELECT EXISTS (SELECT 1 FROM output WHERE type_script_id = $1)"#,
)
...

// line 252: BUG — row_lock used instead of row_type
match row_lock.try_get::<bool, _>(0) {   // ← should be row_type
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [2](#0-1) 

**PostgreSQL path**: `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN`). Because we only reach line 252 when the lock-script check was `false`, this always returns `Ok(false)` — the type-script query result is completely ignored.

**SQLite path**: `row_lock.try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err` arm executes `row_type.get::<i64, _>(0) == 1`, which is correct. SQLite is unaffected.

The caller in `rollback_block` uses the return value to decide whether to delete the script: [3](#0-2) 

When `script_exists_in_output` incorrectly returns `false`, the script ID is appended to `script_id_list_to_remove` and permanently deleted from the `script` table.

### Impact Explanation

On PostgreSQL, every block rollback (chain reorganization) will silently delete any `script` row that is referenced **only** as a `type_script_id` (not as a `lock_script_id`) in the remaining outputs. This permanently corrupts the rich-indexer database:

- Type-script metadata for live cells is erased; subsequent RPC queries (`get_cells`, `get_transactions`, `get_cells_capacity`) that filter or return type-script fields will return incorrect or empty results.
- Applications tracking UDT/NFT assets (which rely on type scripts) will observe phantom asset disappearances after any reorg.
- The corruption is silent — no error is returned, `rollback_block` completes successfully, and the node continues operating with a silently broken index.

### Likelihood Explanation

- Chain reorganizations are a normal, frequent network event; any reorg on a PostgreSQL-backed node triggers the bug.
- No special attacker capability is required: a natural 1-block reorg (common during normal mining) is sufficient.
- The PostgreSQL backend is an explicitly supported deployment option for the rich-indexer.
- The bug is deterministic: every reorg that touches a block containing cells with type scripts will corrupt the index.

### Recommendation

**Short term**: Replace `row_lock` with `row_type` on line 252:

```rust
// correct fix
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

**Long term**: Add an integration test that performs a block rollback on a PostgreSQL backend and verifies that type-script-only scripts are preserved when still referenced by surviving outputs.

### Proof of Concept

1. Deploy a CKB node with the rich-indexer configured to use PostgreSQL.
2. Submit a transaction whose output cell has a type script but whose lock script hash does not appear in any other output.
3. Wait for the block containing that transaction to be confirmed.
4. Trigger a 1-block reorg (e.g., by mining a competing block at the same height).
5. `rollback_block` is called; `script_exists_in_output` is invoked for the type script ID.
6. Because `row_lock.try_get::<bool, _>(0)` returns `Ok(false)` (the script is not a lock script), the function returns `Ok(false)`.
7. The type script row is added to `script_id_list_to_remove` and deleted from the `script` table.
8. Query `get_cells` for the cell's type script hash — the indexer returns no results even though the cell is live on the canonical chain. [4](#0-3)

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
