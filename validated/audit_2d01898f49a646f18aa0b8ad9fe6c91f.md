The bug is confirmed in the actual code. Line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. [1](#0-0) 

Now assessing impact against the allowed bounty scope:

The rich indexer is an optional indexing component — not the core CKB node, not consensus, not CKB-VM. The bug silently corrupts the PostgreSQL-backed indexer's script table during reorgs, causing `get_cells`/`get_cells_capacity` to return wrong results. It does not crash the node, does not affect consensus, does not damage the CKB economy on-chain, and does not affect CKB-VM or system scripts.

The closest allowed impact is **Medium — "Suboptimal implementation of CKB state storage mechanism"** (2001–10000 points). The rich indexer is CKB's indexed state storage layer, and this bug corrupts that state silently on PostgreSQL during reorgs.

---

Audit Report

## Title
`script_exists_in_output` re-reads `row_lock` instead of `row_type` on PostgreSQL, corrupting indexer script state during rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the second `match` block at line 252 erroneously calls `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds, so `row_type` is never consulted. This causes `rollback_block` to delete `script` table rows that are still referenced by surviving outputs' `type_script_id` columns, silently corrupting the indexer's live-cell view.

## Finding Description
`rollback_block` (lines 7–52) calls `script_exists_in_output(script_id, tx)` for every lock and type script of the rolled-back outputs to decide whether to delete the script row. The function runs two SQL `EXISTS` queries: one against `lock_script_id` (result: `row_lock`, lines 208–220) and one against `type_script_id` (result: `row_type`, lines 237–249). The first `match` at line 223 correctly short-circuits on `row_lock`. The second `match` at line 252 is supposed to evaluate `row_type`, but reads `row_lock` again:

```rust
// Line 252 — BUG: row_lock used instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL returns `BOOLEAN`), so the `Ok(r)` arm fires — returning the lock-check result again, completely ignoring `row_type`. On SQLite, `try_get::<bool, _>` fails (SQLite returns `BIGINT`), so it falls through to `row_type.get::<i64, _>(0) == 1`, which is correct. The bug is PostgreSQL-only and silent: no error is returned, the deletion succeeds, and corruption is only discovered when queries return wrong results.

## Impact Explanation
This is a **Medium** severity finding — "Suboptimal implementation of CKB state storage mechanism" (2001–10000 points). The rich indexer is CKB's indexed state storage layer. After the erroneous deletion, the `script` table no longer contains the row for the shared type script. Any subsequent indexer query that joins `output` with `script` on `type_script_id` — including `get_cells` and `get_cells_capacity` — will silently drop or fail to return cells whose type script was deleted. This corrupts the indexer's live-cell view for all users of the PostgreSQL-backed rich indexer. The bug does not affect consensus, the core CKB node, CKB-VM, or on-chain state.

## Likelihood Explanation
Reorgs are a normal part of CKB network operation. The precondition — two blocks sharing a type script where the second is rolled back — is trivially satisfied by any token/UDT deployment (e.g., sUDT, xUDT), where the same type script appears across many outputs in many blocks. No special attacker capability is required; even a natural 1-block reorg suffices. The bug only manifests on PostgreSQL backends.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// Correct fix
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

## Proof of Concept
State-transition test (PostgreSQL):
1. Index Block 1 containing output O1 with `type_script_id = S` (and a different `lock_script_id`).
2. Index Block 2 containing output O2 with `type_script_id = S` (same script row, same `id`).
3. Call `rollback_block` to remove Block 2.
4. Assert: `SELECT COUNT(*) FROM script WHERE id = S` returns `1` — the script must still exist because O1 in Block 1 still references it.
5. On the buggy code (PostgreSQL), the assertion fails: the row is gone, and subsequent `get_cells` for O1 returns no type-script information.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L251-256)
```rust
    // pg type is BOOLEAN
    match row_lock.try_get::<bool, _>(0) {
        Ok(r) => Ok(r),
        // sqlite type is BIGINT
        Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
    }
```
