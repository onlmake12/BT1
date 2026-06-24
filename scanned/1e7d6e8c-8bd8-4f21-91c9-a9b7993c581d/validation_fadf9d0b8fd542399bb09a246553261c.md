The bug is confirmed at line 252 of `util/rich-indexer/src/indexer/remove.rs`. [1](#0-0) 

The second `match` reads `row_lock` instead of `row_type`, and `row_type` is fetched but never evaluated on PostgreSQL. [2](#0-1) 

---

Audit Report

## Title
Copy-paste bug in `script_exists_in_output` causes type scripts to be incorrectly deleted during block rollback on PostgreSQL — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the final `match` at line 252 re-reads `row_lock` instead of `row_type`. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds and always returns `false` at that point (the `true` case was already handled by the early return at line 226), so the type-script existence check is silently skipped. Any script referenced only as a type script in surviving outputs is incorrectly reported as absent and deleted from the `script` table, permanently corrupting the rich indexer's database after any block rollback.

## Finding Description
`script_exists_in_output` performs two SQL `EXISTS` queries:

1. Lines 208–220: `WHERE lock_script_id = $1` → result stored in `row_lock`
2. Lines 237–249: `WHERE type_script_id = $1` → result stored in `row_type`

The first `match` (lines 223–235) correctly evaluates `row_lock` and returns `Ok(true)` early if the lock query is positive. Execution only reaches line 252 when `row_lock` returned `false`. The second `match` at line 252 should evaluate `row_type`, but instead re-evaluates `row_lock`:

```rust
// line 252 — BUG: should be row_type.try_get::<bool, _>(0)
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),                               // always Ok(false) on PG
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1), // correct path, never reached on PG
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL `EXISTS` returns `BOOLEAN`) and always returns `false` here (the `true` case was already handled). `row_type` is fetched but its value is never consulted. On SQLite, `try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err` arm runs and correctly reads `row_type` — SQLite is unaffected.

The caller `rollback_block` (lines 29–38) iterates over all outputs of the rolled-back block and calls `script_exists_in_output` for each `type_script_id`. Because the function always returns `false` for the type-script check on PostgreSQL, every type script from the rolled-back block is unconditionally added to `script_id_list_to_remove` and deleted, even when other surviving outputs still reference the same `type_script_id`.

## Impact Explanation
This is a correctness failure in CKB's rich indexer state storage mechanism. After any block rollback on a PostgreSQL-backed node, the `script` table loses rows for type scripts still referenced by surviving outputs. Subsequent `get_cells` RPC calls join on `script.id`; with the row deleted, those cells become invisible. The corruption is permanent until the indexer is fully rebuilt. This matches the allowed impact: **Medium (2001–10000 points) — Suboptimal/incorrect implementation of CKB state storage mechanism**.

## Likelihood Explanation
Block reorgs (rollbacks) are a normal, frequent occurrence on any live CKB node — short 1-block reorgs happen naturally and can be induced by any miner producing a competing tip. No special privileges are required. Any PostgreSQL-backed rich indexer node that processes a reorg involving transactions with type scripts will trigger this path. The trigger condition is the standard `rollback_block` code path.

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
1. Configure a CKB rich indexer with a PostgreSQL backend.
2. Append a block containing a transaction with at least one output that has a type script (and a different lock script, so `lock_script_id ≠ type_script_id`).
3. Verify the type script row exists in the `script` table: `SELECT * FROM script WHERE id = <type_script_id>`.
4. Trigger a rollback of that block (e.g., by appending a competing block at the same height, causing a reorg).
5. After rollback, query `SELECT * FROM script WHERE id = <type_script_id>` — the row is absent despite the type script still being referenced by any surviving output, confirming the invariant violation.

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L237-256)
```rust
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
```
