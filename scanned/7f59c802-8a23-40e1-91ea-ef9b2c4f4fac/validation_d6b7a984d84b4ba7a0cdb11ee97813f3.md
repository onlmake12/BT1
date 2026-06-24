The code at line 252 is confirmed. Let me analyze it precisely.

The bug is confirmed in the actual code. Line 252 uses `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`.

The execution logic on PostgreSQL:
- Lines 223–235: first match on `row_lock`. If lock check is `true`, return early. If `false`, fall through.
- Line 252: `row_lock.try_get::<bool, _>(0)` is called **again** on `row_lock`. Since we only reach line 252 when `row_lock` already returned `false`, this always returns `Ok(false)` on PostgreSQL — `row_type` is never consulted.

On SQLite, `try_get::<bool, _>(0)` fails (BIGINT type), so the `Err(_)` arm correctly reads `row_type`.

The impact fits "Medium: Suboptimal implementation of CKB state storage mechanism" — the rich indexer is CKB's optional state storage layer, and this bug causes permanent data corruption in it during a routine reorg on PostgreSQL deployments.

---

Audit Report

## Title
PostgreSQL-Only Copy-Paste Bug in `script_exists_in_output` Causes Incorrect Script Deletion During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

## Summary
In `script_exists_in_output`, the final `match` block at line 252 re-evaluates `row_lock` instead of `row_type`. On PostgreSQL, this causes the function to always return the lock-script existence result, completely ignoring the type-script existence result. During `rollback_block`, scripts referenced only as `type_script_id` in surviving outputs are incorrectly deleted, permanently corrupting the indexer's script table until a full resync.

## Finding Description
`script_exists_in_output` (lines 204–257) performs two SQL `EXISTS` queries: one for `lock_script_id` (stored in `row_lock`) and one for `type_script_id` (stored in `row_type`). The first match block (lines 223–235) correctly short-circuits: if the lock check is `true` on PostgreSQL, it returns early. If `false`, execution falls through to fetch `row_type`. However, the second match block at line 252 reads:

```rust
match row_lock.try_get::<bool, _>(0) {   // BUG: should be row_type
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds (BOOLEAN type), returning `Ok(false)` — the same value already established in the first match block (we only reach line 252 when the lock check was `false`). The `row_type` result is never read. The function always returns `false` for any script that is not a `lock_script_id` in surviving outputs.

On SQLite, `try_get::<bool, _>(0)` fails (BIGINT type), so the `Err(_)` arm executes `row_type.get::<i64, _>(0) == 1`, which is correct.

`rollback_block` (lines 7–52) calls this function after deleting the rolled-back block's outputs (line 25). For each deleted output's `type_script_id`, it calls `script_exists_in_output` (lines 33–37). The broken function returns `false` for any script used only as `type_script_id` in surviving outputs, so those scripts are added to `script_id_list_to_remove` and deleted at line 39. Surviving outputs from earlier blocks then hold dangling `type_script_id` foreign-key references. [1](#0-0) [2](#0-1) 

## Impact Explanation
This is a confirmed incorrect implementation of the CKB rich-indexer state storage mechanism (Medium: 2001–10000 points). After the incorrect deletion, the `output` table retains rows with `type_script_id` pointing to deleted `script` rows. Any RPC query joining `output` with `script` on `type_script_id` (e.g., `get_cells`, `get_transactions`, `get_cells_capacity` filtered by type script) returns zero results for affected cells. The corruption is permanent until the indexer is fully reset and resynced from genesis. This is a correctness failure in the PostgreSQL-backed rich indexer's rollback path, a documented and supported CKB state storage deployment.

## Likelihood Explanation
The trigger is a natural blockchain reorg — a routine event requiring no attacker action. Short reorgs (1–2 blocks) occur organically when competing blocks are found near-simultaneously. The only precondition is a PostgreSQL-backed rich indexer deployment (explicitly documented in `util/rich-indexer/README.md`) where the rolled-back block contains outputs whose type script is not also used as a lock script in surviving outputs. This is the common case for UDT/NFT type scripts. The bug is invisible in the default SQLite deployment, meaning it goes undetected until a PostgreSQL operator experiences a reorg. [3](#0-2) 

## Recommendation
Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // fix: was row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Additionally, deduplicate `script_id_list_to_remove` before calling `remove_batch_by_blobs` to avoid redundant deletions when multiple outputs in the same block share a script ID. [1](#0-0) 

## Proof of Concept
1. Start a CKB node with `db_type = "postgres"` in `[indexer_v2.rich_indexer]`.
2. Index block A (height N−1) with output O₀: `type_script_id = S`, `lock_script_id = L₁` (L₁ ≠ S).
3. Index block B (height N) with outputs O₁, O₂: `type_script_id = S`, `lock_script_id = L₂` (L₂ ≠ S).
4. Trigger rollback of block B (simulate a reorg).
5. **Expected:** script S survives — O₀ in block A still references it as `type_script_id`.
6. **Actual (PostgreSQL):** `script_exists_in_output(S)` checks only `lock_script_id = S` (finds nothing), returns `false`, S is deleted. O₀ now has a dangling `type_script_id`.
7. Query `get_cells` with `type_script = S` → returns 0 results instead of 1.
8. Repeat with SQLite → returns 1 result (correct), confirming the differential. [4](#0-3)

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
