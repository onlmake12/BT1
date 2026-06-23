### Title
Wrong Variable Reference in `script_exists_in_output()` Causes Premature Script Deletion During Reorg — (`File: util/rich-indexer/src/indexer/remove.rs`)

### Summary
`script_exists_in_output()` in the rich-indexer's rollback path contains a copy-paste error: the second `match` block re-reads `row_lock` instead of `row_type`. On PostgreSQL, this causes the function to return `false` for any script that is referenced only as a `type_script_id` (not as a `lock_script_id`), making `rollback_block` delete that script from the `script` table even though live outputs still reference it. The result is silent, permanent database corruption of the rich-indexer on every chain reorganization that involves type-scripted cells.

---

### Finding Description

`script_exists_in_output` is called by `rollback_block` to decide whether a script row is still referenced by any surviving output before deleting it. The function runs two SQL `EXISTS` queries — one for `lock_script_id` and one for `type_script_id` — and is supposed to return `true` if either query finds a match.

```rust
// util/rich-indexer/src/indexer/remove.rs  lines 204-257

async fn script_exists_in_output(script_id: i64, ...) -> Result<bool, Error> {
    let row_lock = sqlx::query("SELECT EXISTS (SELECT 1 FROM output WHERE lock_script_id = $1)")
        .bind(script_id).fetch_one(...).await?;

    // ✅ correct: uses row_lock
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => { if r { return Ok(true); } }
        Err(_) => { if row_lock.get::<i64, _>(0) == 1 { return Ok(true); } }
    }

    let row_type = sqlx::query("SELECT EXISTS (SELECT 1 FROM output WHERE type_script_id = $1)")
        .bind(script_id).fetch_one(...).await?;

    // ❌ BUG: uses row_lock again instead of row_type
    match row_lock.try_get::<bool, _>(0) {   // <── should be row_type
        Ok(r) => Ok(r),
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
}
```

On **PostgreSQL** `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN`), so the `Ok(r)` arm is taken and the function returns the **lock-script** existence result a second time, completely ignoring `row_type`. On **SQLite** `try_get::<bool, _>` fails (SQLite returns `BIGINT`), so the `Err` arm falls through to `row_type.get::<i64, _>(0)`, which is correct — SQLite is unaffected.

---

### Impact Explanation

`rollback_block` uses the return value to gate deletion:

```rust
// lines 29-38
for (_, lock_script_id, type_script_id) in output_lock_type_list {
    if !script_exists_in_output(lock_script_id, tx).await? {
        script_id_list_to_remove.push(lock_script_id);
    }
    if let Some(type_script_id) = type_script_id
        && !script_exists_in_output(type_script_id, tx).await?
    {
        script_id_list_to_remove.push(type_script_id);   // ← wrongly added
    }
}
remove_batch_by_blobs("script", "id", &script_id_list_to_remove, tx).await?;
```

When a reorg rolls back a block that contains outputs with a type script that is **not** simultaneously used as a lock script in any surviving output, `script_exists_in_output` returns `false` for that type script ID (because it re-checks the lock-script query). The script row is then deleted from the `script` table even though other outputs still hold a foreign-key reference to it. This silently corrupts the rich-indexer database: subsequent RPC queries that join on `script_id` (e.g., `get_cells`, `get_transactions`) will return incomplete or missing results for any cell whose type script was incorrectly purged.

**Impact: Medium** — Persistent, silent data corruption of the rich-indexer on PostgreSQL; incorrect RPC query results for type-scripted cells after any reorg.

---

### Likelihood Explanation

Chain reorganizations are a routine, externally-triggerable event: any peer that relays a competing chain of sufficient work causes a reorg and invokes `rollback_block`. Type scripts are ubiquitous on CKB (UDT, NFT, DAO, etc.). No special privilege is required; a normal sync peer or block relayer is sufficient to trigger the path. On a PostgreSQL-backed rich-indexer node, every reorg involving type-scripted cells will silently corrupt the database.

**Likelihood: High**

---

### Recommendation

Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // ← was row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

---

### Proof of Concept

**Root cause — exact line:** [1](#0-0) 

The second `match` block at line 252 reads `row_lock` (the lock-script query result) instead of `row_type` (the type-script query result). On PostgreSQL the `Ok` arm always succeeds, so `row_type` is never consulted.

**Caller that acts on the wrong return value:** [2](#0-1) 

`rollback_block` deletes any script for which `script_exists_in_output` returns `false`. Because the function returns the lock-script result for both checks on PostgreSQL, a script used only as a type script is incorrectly reported as absent and is deleted.

**Entry path — reorg triggers rollback:** [3](#0-2) 

`rollback_block` is the top-level rollback entry point called on every chain reorganization. Any block relayer or sync peer that delivers a heavier competing chain causes a reorg and reaches this code path without any privileged access.

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

**File:** util/rich-indexer/src/indexer/remove.rs (L251-256)
```rust
    // pg type is BOOLEAN
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```
