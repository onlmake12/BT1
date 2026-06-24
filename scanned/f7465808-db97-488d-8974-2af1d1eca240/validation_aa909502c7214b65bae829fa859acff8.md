The code at line 252 confirms the bug exactly as described — `row_lock.try_get::<bool, _>(0)` is used instead of `row_type.try_get::<bool, _>(0)`. [1](#0-0) 

At that point in the function, `row_lock` is already known to be `false` (the early-return guard at lines 223–235 would have fired otherwise), so on PostgreSQL the `Ok(r)` branch always returns `Ok(false)`, ignoring `row_type` entirely. [2](#0-1) 

---

Audit Report

## Title
Wrong Variable Reference in `script_exists_in_output` Causes Silent Script Record Deletion for Type-Script-Only Scripts on PostgreSQL — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the final `match` block at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, `row_lock` always decodes successfully as `bool` and is always `false` at that point (the early-return guard above would have fired otherwise), so the function unconditionally returns `Ok(false)` for any script that is referenced only as a type script. During block removal (reorg), the caller interprets this as "no output references this script" and deletes the script record from the indexer's database, corrupting the rich indexer's state for all PostgreSQL-backed deployments.

## Finding Description
`script_exists_in_output` performs two sequential `EXISTS` queries:
1. `row_lock` — checks `lock_script_id = $1`
2. `row_type` — checks `type_script_id = $1`

If `row_lock` is `true`, the function returns `Ok(true)` early (lines 223–235). Otherwise it queries `row_type` (lines 237–249) and is supposed to return that result. The final `match` at line 252 is a copy-paste of the first `match` and still references `row_lock` instead of `row_type`. On PostgreSQL, `EXISTS` returns `BOOLEAN`, so `row_lock.try_get::<bool, _>(0)` always succeeds with `r = false` (since the early-return guard already handled the `true` case), and the function returns `Ok(false)` regardless of `row_type`'s value. On SQLite, `EXISTS` returns `BIGINT`, so `try_get::<bool, _>(0)` fails and the `Err` branch correctly reads `row_type.get::<i64, _>(0)`, making this bug SQLite-silent.

## Impact Explanation
This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**. The rich indexer is CKB's state storage layer for RPC consumers. The bug causes silent, incorrect deletion of script records from the indexer's PostgreSQL database during reorgs, permanently corrupting the indexer's view of chain state. Subsequent `get_cells` and `get_transactions` RPC calls for cells referencing the deleted type script return empty or incorrect results, making the indexer's data unreliable for any PostgreSQL-backed deployment.

## Likelihood Explanation
No special attacker capability is required. Any transaction that attaches a type script to an output without using that same script as a lock script (the standard pattern for UDT/NFT cells) will trigger this path during a reorg on PostgreSQL. Reorgs are a normal part of chain operation. The bug is deterministic and repeatable on every affected reorg event.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // was: row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

## Proof of Concept
1. Deploy a PostgreSQL-backed CKB rich indexer node.
2. Submit a transaction whose output carries a type script `S` with a different lock script (standard UDT cell pattern).
3. Mine the block containing that transaction.
4. Trigger a reorg removing that block (e.g., by mining a longer competing chain from before that block).
5. During removal, `script_exists_in_output` is called for script `S`:
   - `row_lock` query returns `EXISTS = false` (S is not a lock script) → early-return guard not triggered.
   - `row_type` query returns `EXISTS = true` (S is a type script).
   - Final `match` reads `row_lock.try_get::<bool, _>(0)` → `Ok(false)`.
   - Function returns `Ok(false)` — incorrect.
6. Caller deletes script `S` from the script table despite it still being referenced.
7. `get_cells` / `get_transactions` RPC calls for cells using script `S` return empty or corrupt results.

### Citations

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
