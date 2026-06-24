The bug at line 252 is confirmed in the actual source code.

Audit Report

## Title
Copy-Paste Bug in `script_exists_in_output` Silently Skips Type Script Existence Check on PostgreSQL, Causing Incorrect Script Deletion During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
At line 252 of `script_exists_in_output`, the code re-evaluates `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, this causes the function to always return `false` for any script referenced only as a `type_script_id` in surviving outputs, because `row_lock` was already confirmed false by the first match block. During `rollback_block`, this causes still-referenced type scripts to be incorrectly deleted from the `script` table, permanently corrupting the rich indexer's relational state.

## Finding Description
`script_exists_in_output` performs two sequential SQL queries:

1. `row_lock`: `SELECT EXISTS (... WHERE lock_script_id = $1)` — checked at lines 223–235. If true, returns `Ok(true)` early. If false, execution continues.
2. `row_type`: `SELECT EXISTS (... WHERE type_script_id = $1)` — fetched at lines 237–249.

The second `match` at line 252 is the copy-paste bug:

```rust
// WRONG — re-reads row_lock, not row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [1](#0-0) 

**On PostgreSQL**, `EXISTS` returns `BOOLEAN`, so `row_lock.try_get::<bool, _>(0)` succeeds. Since the first match already confirmed `row_lock` is `false` (otherwise `Ok(true)` would have been returned at line 226), the second match also evaluates `row_lock` as `false` and returns `Ok(false)` — `row_type` is never inspected.

**On SQLite**, `EXISTS` returns `BIGINT`, so `row_lock.try_get::<bool, _>(0)` fails, the `Err(_)` arm executes, and `row_type.get::<i64, _>(0) == 1` is correctly evaluated. SQLite is unaffected.

The caller `rollback_block` deletes outputs first (line 25), then calls `script_exists_in_output` to determine which scripts are safe to delete: [2](#0-1) 

On PostgreSQL, any type script that is exclusively referenced as a `type_script_id` (not as a `lock_script_id`) in surviving outputs will have `script_exists_in_output` return `false`, causing it to be pushed into `script_id_list_to_remove` and deleted from the `script` table even though it is still in use.

## Impact Explanation
This is a **Medium** severity finding: incorrect implementation of the CKB state storage mechanism (rich indexer). After a reorg on a PostgreSQL-backed rich indexer:

- Type scripts still referenced by surviving outputs are deleted from the `script` table.
- Subsequent `get_cells` queries that JOIN `output` against `script` produce `NULL` type script fields or miss cells entirely.
- The corruption is permanent until the indexer is fully rebuilt from scratch.

This does not affect consensus, the core CKB node process, or the broader network. The impact is confined to the rich indexer's relational state and the correctness of its RPC responses (`get_cells`). This matches **Medium (2001–10000 points): Suboptimal/incorrect implementation of CKB state storage mechanism**.

## Likelihood Explanation
The preconditions are: (1) the operator uses the PostgreSQL backend (a documented, supported production configuration per `util/rich-indexer/README.md`), and (2) a chain reorg occurs. Reorgs are a normal, unprivileged network event — any peer can relay a valid competing chain of sufficient work. No special privileges, leaked keys, or majority hashpower are required. Any type script that is not simultaneously used as a lock script in surviving outputs will be incorrectly deleted on every reorg.

## Recommendation
Change line 252 from `row_lock` to `row_type`:

```rust
// CORRECT
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [3](#0-2) 

## Proof of Concept
1. Start a CKB node with `--rich-indexer` and PostgreSQL backend.
2. Index a block containing two outputs sharing the same `type_script` T1 but different `lock_scripts` (output1: lock=L1, type=T1; output2: lock=L2, type=T1).
3. Index a second block containing one output (output3: lock=L3, type=T1).
4. Trigger rollback of block 2 (simulate a reorg by replacing it with a competing block).
5. Query the `script` table: T1's row will be absent even though output1 and output2 in block 1 still reference it.
6. Call `get_cells` filtering by T1 — results will have `NULL` type script data or return no results, confirming the corruption.

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
