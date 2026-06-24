Audit Report

## Title
Incorrect `row_lock` read in `script_exists_in_output` causes type_script deletion during rollback on PostgreSQL — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, after fetching the `type_script_id` EXISTS result into `row_type`, line 252 mistakenly reads `row_lock` instead of `row_type`. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (PG returns BOOLEAN), so the function returns the lock_script existence result for the type_script check. This causes incorrect deletion of type_scripts still referenced by surviving outputs after a rollback, corrupting the rich-indexer DB.

## Finding Description
`script_exists_in_output` (lines 204–257) performs two EXISTS queries: one on `lock_script_id` (result in `row_lock`, lines 208–220) and one on `type_script_id` (result in `row_type`, lines 237–249). The early-return logic at lines 222–235 correctly uses `row_lock` for the lock check. However, the final match at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. [1](#0-0) 

On PostgreSQL, `try_get::<bool, _>(0)` on `row_lock` always returns `Ok(r)` (PG EXISTS returns BOOLEAN), so the `Err(_)` branch — which correctly uses `row_type` — is never reached. The function returns the lock_script EXISTS result for the type_script check. On SQLite, `try_get::<bool, _>(0)` fails (SQLite EXISTS returns BIGINT), so it falls through to `row_type.get::<i64, _>(0) == 1`, which is correct.

Concrete failure path during rollback (`rollback_block`, lines 27–39):
1. Outputs of the rolled-back block are deleted from the `output` table (line 25). [2](#0-1) 
2. For each `type_script_id = X` where `X` is not shared as any `lock_script_id`, `script_exists_in_output(X)` is called. [3](#0-2) 
3. `row_lock` query: `EXISTS(... WHERE lock_script_id = X)` → `false`.
4. `row_type` query: `EXISTS(... WHERE type_script_id = X)` → `true` (surviving outputs still reference X).
5. Line 252 reads `row_lock` → returns `Ok(false)` → `X` is pushed to `script_id_list_to_remove`. [4](#0-3) 
6. `remove_batch_by_blobs("script", "id", ...)` deletes script `X`. [5](#0-4) 
7. Surviving outputs retain `type_script_id = X` pointing to a now-deleted row.
8. Subsequent RPC calls (`get_cells`, `get_transactions`) that JOIN on the `script` table encounter DB errors.

The early-return at lines 222–235 only fires when the lock check is `true`, providing no guard against this path. [6](#0-5) 

## Impact Explanation
This causes local RPC API crashes (`get_cells`, `get_transactions`) on any PostgreSQL-backed rich-indexer node that processes a reorg involving outputs with type_scripts not shared as lock_scripts. The indexer DB is silently corrupted and requires manual repair or full re-sync. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**.

## Likelihood Explanation
Reorgs are a normal part of CKB network operation and require no attacker. Any operator running the rich-indexer with PostgreSQL will trigger this bug on the first reorg involving outputs with unique type_scripts, which is a common real-world scenario. No privileged access or special conditions are required.

## Recommendation
Change line 252 from:
```rust
match row_lock.try_get::<bool, _>(0) {
```
to:
```rust
match row_type.try_get::<bool, _>(0) {
```

## Proof of Concept
1. Start a rich-indexer node with PostgreSQL backend.
2. Append a block containing an output with a `type_script` whose `id` does not appear as any `lock_script_id` in the DB.
3. Trigger a rollback (reorg) of that block.
4. Call `get_cells` or `get_transactions` referencing any script from the rolled-back block.
5. Observe a DB error due to the missing `script` row.

Differential test: run the same sequence against SQLite — no error occurs, confirming the PG-specific divergence caused by the `row_lock`/`row_type` mix-up at line 252.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L25-25)
```rust
    remove_batch_by_blobs("output", "tx_id", &tx_id_list, tx).await?;
```

**File:** util/rich-indexer/src/indexer/remove.rs (L33-37)
```rust
        if let Some(type_script_id) = type_script_id
            && !script_exists_in_output(type_script_id, tx).await?
        {
            script_id_list_to_remove.push(type_script_id);
        }
```

**File:** util/rich-indexer/src/indexer/remove.rs (L39-39)
```rust
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
