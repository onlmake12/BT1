The bug is confirmed in the actual code. Line 252 of `util/rich-indexer/src/indexer/remove.rs` reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`.

Audit Report

## Title
Wrong Variable Reference in `script_exists_in_output()` Causes Premature Script Deletion During Reorg — (`File: util/rich-indexer/src/indexer/remove.rs`)

## Summary
`script_exists_in_output` at line 252 of `util/rich-indexer/src/indexer/remove.rs` reads `row_lock` instead of `row_type` in the second `match` block. On PostgreSQL, where `SELECT EXISTS` returns a native `BOOLEAN`, the `try_get::<bool, _>(0)` call on `row_lock` always succeeds, causing the function to return the lock-script existence result for both checks and never consult the type-script query result. `rollback_block` then incorrectly deletes type-script rows from the `script` table even when surviving outputs still reference them, silently corrupting the rich-indexer database on every reorg involving type-scripted cells.

## Finding Description
`script_exists_in_output` (lines 204–257) runs two `SELECT EXISTS` queries — one against `lock_script_id`, one against `type_script_id` — and is supposed to return `true` if either finds a match. The first `match` block (line 223) correctly reads `row_lock`. After fetching `row_type` (line 237), the second `match` block (line 252) mistakenly reads `row_lock` again:

```rust
// line 252 — BUG: row_lock should be row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),                              // always taken on PostgreSQL
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL returns `BOOLEAN`), so `row_type` is never read. The function returns the lock-script result for both the lock and type checks. On SQLite, `try_get::<bool, _>` fails (SQLite returns `BIGINT`), so the `Err` arm correctly falls through to `row_type.get::<i64, _>(0)` — SQLite is unaffected.

`rollback_block` (lines 29–38) calls `script_exists_in_output` to gate deletion:

```rust
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
```

When a reorg rolls back a block containing outputs whose type script is not simultaneously used as a lock script in any surviving output, `script_exists_in_output` returns `false` for that type script ID (because it re-evaluates the lock-script query). The script row is then passed to `remove_batch_by_blobs("script", ...)` and permanently deleted, even though other outputs still hold a foreign-key reference to it.

## Impact Explanation
The rich-indexer is a CKB state storage mechanism. Silent deletion of referenced `script` rows corrupts the indexer's relational state: subsequent RPC queries that join on `script_id` (e.g., `get_cells`, `get_transactions`) return incomplete or missing results for any cell whose type script was incorrectly purged. The corruption is permanent and accumulates across reorgs. This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation
Chain reorganizations are a routine, externally-triggerable event requiring no special privilege — any peer relaying a competing chain of sufficient work causes a reorg and invokes `rollback_block`. Type scripts are ubiquitous on CKB (UDT, NFT, DAO, etc.). On any PostgreSQL-backed rich-indexer node, every reorg involving type-scripted cells will silently corrupt the database. No victim mistake or unrealistic precondition is required.

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // ← was row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

## Proof of Concept
1. Stand up a PostgreSQL-backed rich-indexer node.
2. Index a block containing at least one output with a type script that is not used as a lock script in any other surviving output.
3. Trigger a reorg that rolls back that block (e.g., by feeding a competing chain of equal or greater work).
4. Query the `script` table: the type script row will be absent even though outputs in earlier blocks still reference it via `type_script_id`.
5. Issue a `get_cells` RPC filtered by that type script: results will be empty or incomplete, confirming the corruption.

A targeted unit test can reproduce this on PostgreSQL by: (a) inserting a block with a type-scripted output, (b) calling `rollback_block`, and (c) asserting that the type script row still exists in the `script` table — this assertion will fail with the current code.