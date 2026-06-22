The bug is real and confirmed in the code. Let me analyze it precisely.

The bug is confirmed. Here is the precise analysis:

**Root cause at line 252:** `row_lock.try_get::<bool, _>(0)` is used instead of `row_type.try_get::<bool, _>(0)`.

**Execution path through the function:**

1. Lines 208–220: First query checks `lock_script_id = $1` → result in `row_lock`
2. Lines 223–235: If `row_lock` is `true`, early-return `Ok(true)` — correct
3. If `row_lock` is `false`, execution falls through to the second query
4. Lines 237–249: Second query checks `type_script_id = $1` → result in `row_type`
5. **Line 252**: `match row_lock.try_get::<bool, _>(0)` — re-reads `row_lock` (always `false` at this point) instead of `row_type`

**Database-specific behavior:**

- **PostgreSQL**: `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN`), so `Ok(r)` is returned where `r` is the stale `row_lock` value — which is always `false` here (the `true` case already returned early). The `row_type` result is completely ignored.
- **SQLite**: `row_lock.try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), falls to `Err(_)` branch which correctly reads `row_type.get::<i64, _>(0) == 1`. SQLite is unaffected.

**Consequence on PostgreSQL during `rollback_block`:**

At the call site (lines 33–37 of `remove.rs`):

```rust
if let Some(type_script_id) = type_script_id
    && !script_exists_in_output(type_script_id, tx).await?
{
    script_id_list_to_remove.push(type_script_id);
}
```

`script_exists_in_output(type_script_id)` always returns `false` on PostgreSQL (unless the type script also happens to be used as a lock script somewhere, which is essentially never). Every type script from the rolled-back block is unconditionally pushed into `script_id_list_to_remove` and deleted — even if it is still referenced by outputs in the surviving chain.

**Trigger path:**

Any reorg triggers `IndexerSync::try_loop_sync` (line 170 of `util/indexer-sync/src/lib.rs`) → `indexer.rollback()` → `AsyncRichIndexer::rollback` (line 187 of `util/rich-indexer/src/indexer/mod.rs`) → `rollback_block` → `script_exists_in_output`. Reorgs are a normal part of blockchain operation; no attacker is required. A peer relaying a valid competing chain with more cumulative PoW is sufficient.

**Why existing tests don't catch this:**

All rollback tests in `util/rich-indexer/src/tests/rollback.rs` use SQLite (`connect_sqlite(MEMORY_DB)`). The SQLite code path at line 255 correctly reads `row_type`, so the bug is invisible in the test suite.

---

### Title
`script_exists_in_output` reads stale `row_lock` instead of `row_type` on PostgreSQL, permanently deleting referenced type scripts during rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary
A copy-paste error on line 252 of `script_exists_in_output` causes the function to re-evaluate the lock-script query result (`row_lock`) instead of the type-script query result (`row_type`) when determining whether a type script is still referenced. On PostgreSQL this always returns `false`, so every type script belonging to a rolled-back block is unconditionally deleted from the `script` table, even when it is still referenced by outputs in the surviving chain.

### Finding Description
In `util/rich-indexer/src/indexer/remove.rs`, `script_exists_in_output` executes two SQL `EXISTS` queries: one against `lock_script_id` (result: `row_lock`) and one against `type_script_id` (result: `row_type`). The first match arm at lines 223–235 correctly short-circuits when `row_lock` is true. However, the final match arm at line 252 mistakenly re-reads `row_lock` instead of `row_type`: [1](#0-0) 

Because execution only reaches line 252 when `row_lock` is already `false` (the `true` case returned early at line 226), the PostgreSQL branch always returns `Ok(false)`, making the type-script existence check a no-op. The SQLite branch at line 255 is correct (`row_type.get`) and is unaffected. [2](#0-1) 

### Impact Explanation
On any PostgreSQL-backed rich-indexer deployment, every reorg permanently deletes all type scripts that were exclusively used in the rolled-back block's outputs, regardless of whether those scripts are still referenced by surviving outputs. After the corruption, `get_cells` and `get_transactions` queries filtered by those type scripts return empty results. The corruption persists until a full re-index is performed.

### Likelihood Explanation
Reorgs are a routine occurrence on mainnet (natural chain reorganizations). No attacker action is required; the bug fires on every rollback of a block containing type-scripted outputs on a PostgreSQL deployment. An adversary can also deliberately induce a reorg by mining a short competing chain.

### Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Add a PostgreSQL-backed integration test for rollback that verifies the `script` table count after rolling back a block containing type-scripted outputs that are also present in the surviving chain.

### Proof of Concept
1. Start a PostgreSQL-backed rich-indexer.
2. Append block A containing output O with `type_script_id = S` (where S is not used as a lock script anywhere).
3. Append block B (also containing an output with `type_script_id = S`).
4. Trigger rollback of block B.
5. Query `SELECT COUNT(*) FROM script WHERE id = S` — returns 0 (script deleted).
6. Query `get_cells` filtered by type script S — returns empty, despite S still being referenced by block A's output.
7. Fix line 252 to use `row_type`, repeat — script S is retained, `get_cells` returns the correct cell. [3](#0-2) [4](#0-3)

### Citations

**File:** util/rich-indexer/src/indexer/remove.rs (L29-38)
```rust
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

**File:** util/rich-indexer/src/tests/rollback.rs (L1-10)
```rust
use super::*;

use tokio::test;

#[test]
async fn test_rollback_block_0() {
    let storage = connect_sqlite(MEMORY_DB).await;
    let indexer = AsyncRichIndexer::new(
        storage.clone(),
        None,
```
