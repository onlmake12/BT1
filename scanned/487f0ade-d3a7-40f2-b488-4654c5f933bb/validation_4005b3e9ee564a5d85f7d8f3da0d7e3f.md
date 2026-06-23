The bug is real and clearly present in the code. Let me confirm the exact details before rendering a verdict.

The `script_exists_in_output` function at lines 204–257 of `util/rich-indexer/src/indexer/remove.rs` contains a copy-paste error:

- Line 208–220: `row_lock` is fetched via `WHERE lock_script_id = $1`
- Line 223: First `match` correctly reads `row_lock.try_get::<bool, _>(0)` — if true, returns early
- Line 237–249: `row_type` is fetched via `WHERE type_script_id = $1`
- **Line 252**: Second `match` reads `row_lock.try_get::<bool, _>(0)` — **should be `row_type`** [1](#0-0) 

On **PostgreSQL**, `row_lock.try_get::<bool, _>(0)` succeeds (returns `Ok(false)` when the script is not a lock script), so the `Ok(r)` arm at line 253 fires and returns `Ok(false)` — completely ignoring `row_type`. The `row_type` result is only used in the `Err(_)` branch (line 255), which is the SQLite path (SQLite returns BIGINT, not BOOLEAN, so `try_get::<bool,_>` fails there). [2](#0-1) 

On **SQLite**, `try_get::<bool, _>(0)` always fails, so execution always falls to the `Err(_)` branch which correctly reads `row_type.get::<i64, _>(0) == 1`. SQLite is unaffected.

The caller in `rollback_block` uses the return value to decide whether to delete the script: [3](#0-2) 

---

### Title
PostgreSQL-only copy-paste bug in `script_exists_in_output` causes premature deletion of type-script rows during rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary
In `script_exists_in_output`, the final `match` at line 252 reads `row_lock` instead of `row_type`. On PostgreSQL, this means the type-script existence check always returns the lock-script query result, so any script used exclusively as a `type_script_id` (never as a `lock_script_id`) is incorrectly deemed absent and deleted from the `script` table during rollback, even when other outputs in earlier blocks still reference it.

### Finding Description
`script_exists_in_output` runs two SQL `EXISTS` queries: one against `lock_script_id` (stored in `row_lock`) and one against `type_script_id` (stored in `row_type`). The first `match` (line 223) correctly short-circuits on `row_lock`. The second `match` (line 252) is supposed to evaluate `row_type` but mistakenly re-reads `row_lock`. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds, so `row_type` is never consulted. The function returns `Ok(false)` for any script that is not a lock script, regardless of whether it is still referenced as a type script.

`rollback_block` calls this function for every `type_script_id` in the rolled-back outputs and pushes the ID into `script_id_list_to_remove` when it returns `false`. The subsequent `remove_batch_by_blobs("script", ...)` then deletes the row permanently. [4](#0-3) 

### Impact Explanation
After the script row is deleted, any RPC call that queries cells or transactions by that type script (e.g., `get_cells`, `get_transactions`) returns empty results, even though the corresponding outputs still exist on-chain and in the `output` table. The indexer state permanently diverges from chain state; the only recovery is a full re-sync of the rich indexer from scratch.

### Likelihood Explanation
Chain reorganizations are a normal part of CKB operation. Any block producer (miner) can create a block containing outputs whose type script is shared with outputs in earlier blocks and whose script is never used as a lock script. When that block is later rolled back (naturally via reorg, or via the indexer's own rollback path during sync), the bug fires. No special privileges are required beyond the ability to submit a valid block.

### Recommendation
Change line 252 from:
```rust
match row_lock.try_get::<bool, _>(0) {
```
to:
```rust
match row_type.try_get::<bool, _>(0) {
``` [5](#0-4) 

### Proof of Concept
1. Start a CKB rich-indexer with a PostgreSQL backend.
2. Append block A containing output O1 with `type_script_id = S` (script S is not used as any lock script).
3. Append block B containing output O2 also with `type_script_id = S`.
4. Trigger rollback of block B (e.g., via a reorg).
5. Observe: `script_exists_in_output(S)` queries `lock_script_id = S` → `EXISTS = false` → `row_lock` holds `false`. Then queries `type_script_id = S` → `EXISTS = true` → `row_type` holds `true`. But line 252 reads `row_lock` again → returns `Ok(false)`. S is added to `script_id_list_to_remove` and deleted.
6. Assert: the `script` table no longer contains row S, yet O1 still exists in `output` with `type_script_id = S`. `get_cells` for script S returns 0 results. Invariant violated.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L27-39)
```rust
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
