There is a real bug here, but the root cause is not the duplicate-in-list issue — it is a copy-paste error in `script_exists_in_output` that only manifests on PostgreSQL.

---

### Title
PostgreSQL-only: `script_exists_in_output` reads `row_lock` instead of `row_type` in final branch, causing incorrect deletion of type scripts during rollback — (`util/rich-indexer/src/indexer/remove.rs`)

---

### Finding Description

In `script_exists_in_output`, the function fetches two rows:

- `row_lock` — result of `SELECT EXISTS (... WHERE lock_script_id = $1)`
- `row_type` — result of `SELECT EXISTS (... WHERE type_script_id = $1)`

The early-return path (lines 223–235) is correct: if `row_lock` is true, return `true` immediately.

The final return path (lines 252–256) is **wrong**:

```rust
// pg type is BOOLEAN
match row_lock.try_get::<bool, _>(0) {   // ← BUG: should be row_type
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [1](#0-0) 

On **PostgreSQL**, `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN`). But we only reach this branch when `row_lock` was already `false` (the lock-script check failed). So the match arm `Ok(r)` returns `Ok(false)` — **ignoring `row_type` entirely**.

On **SQLite**, `row_lock.try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err(_)` arm runs and correctly reads `row_type`.

The result: on PostgreSQL, `script_exists_in_output` **always returns `false`** for any script that is referenced only as a `type_script_id` (not as a `lock_script_id`). The function never consults `row_type` on PostgreSQL.

---

### Impact Explanation

In `rollback_block`, outputs from the tip block are deleted first (line 25), then `script_exists_in_output` is called for each script to decide whether to delete it: [2](#0-1) 

Because `script_exists_in_output` always returns `false` for type-script-only scripts on PostgreSQL, any script that:
1. appears as `type_script_id` in the rolled-back block's outputs, **and**
2. is also referenced as `type_script_id` in surviving outputs from earlier blocks

…will be incorrectly added to `script_id_list_to_remove` and deleted. The surviving outputs still hold a foreign-key reference to the now-deleted script row. All indexer RPC queries that join on `script` (e.g., `get_cells`, `get_transactions` filtered by type script) will return empty results for those cells — permanently, until the indexer is rebuilt.

The duplicate-in-list issue the question raises (`script_id_list_to_remove` containing the same ID N times) is real but harmless: SQL `DELETE … WHERE id IN (X, X, X)` is idempotent. The actual damage comes solely from the `row_lock`/`row_type` confusion.

---

### Likelihood Explanation

- Requires the node operator to use PostgreSQL (not SQLite). This is a supported and documented deployment option.
- Requires a chain reorg (rollback of at least one block). Reorgs are a normal network event; no attacker capability is needed beyond the natural operation of the CKB P2P network.
- Requires the rolled-back block to contain at least one output whose `type_script_id` is also referenced by an output in an earlier block. This is a common pattern (e.g., any token/UDT type script used across multiple blocks).

All three conditions are routinely met in production.

---

### Recommendation

Fix line 252 to use `row_type` instead of `row_lock`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // ← was row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [1](#0-0) 

Additionally, deduplicate `script_id_list_to_remove` before calling `remove_batch_by_blobs` to avoid generating unnecessarily large SQL `IN` clauses when many outputs share a script.

---

### Proof of Concept

Differential test (SQLite vs PostgreSQL):

1. Index block 0 with outputs O1 and O2, both having `type_script_id = S` (and distinct lock scripts).
2. Index block 1 with output O3 also having `type_script_id = S`.
3. Roll back block 1.
4. Query the `script` table for `id = S`.
   - **SQLite**: script S still exists (correct).
   - **PostgreSQL**: script S is deleted (bug), even though O1 and O2 in block 0 still reference it.
5. Query the indexer RPC for cells with type script S — returns empty on PostgreSQL.

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

**File:** util/rich-indexer/src/indexer/remove.rs (L251-256)
```rust
    // pg type is BOOLEAN
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```
