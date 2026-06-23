### Title
Wrong Variable Reference in `script_exists_in_output` Always Returns `false` for Type-Script-Only Scripts on PostgreSQL — (`util/rich-indexer/src/indexer/remove.rs`)

---

### Summary

In `script_exists_in_output`, after querying whether a script is referenced as a `type_script_id` in any output, the final `match` block at line 252 incorrectly reads from `row_lock` instead of `row_type`. On PostgreSQL this causes the function to always return `false` for any script that is not a lock script, even when it is actively referenced as a type script — directly analogous to the external report's "missing return true" pattern where a branch executes correctly but returns the wrong success indicator.

---

### Finding Description

`script_exists_in_output` is called during block-removal (reorg) to decide whether a script record can be safely deleted from the indexer's script table. It performs two sequential SQL `EXISTS` queries:

1. `row_lock` — does any output reference this script as `lock_script_id`?
2. `row_type` — does any output reference this script as `type_script_id`?

If `row_lock` is true the function returns early with `Ok(true)`. Otherwise it falls through to query `row_type` and is supposed to return that result. The bug is in the final `match`:

```rust
// line 252 — BUG: row_lock used instead of row_type
match row_lock.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

On **PostgreSQL**, `row_lock.try_get::<bool, _>(0)` always succeeds (PostgreSQL returns `BOOLEAN`). At this point in the code `row_lock` is already known to be `false` (the early-return guard above would have fired otherwise), so `r = false` and the function returns `Ok(false)` — ignoring `row_type` entirely.

On **SQLite**, `try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err` branch is taken and `row_type` is read correctly.

The correct code at line 252 should be `row_type.try_get::<bool, _>(0)`. [1](#0-0) 

---

### Impact Explanation

When a script is used **only** as a type script (not as a lock script) and a reorg removes the block that introduced it, `script_exists_in_output` returns `false` on PostgreSQL. The caller interprets this as "no output references this script" and may delete the script record from the database. Subsequent RPC queries for cells or transactions that reference that type script will return incorrect or missing results, corrupting the rich indexer's view of chain state for any PostgreSQL-backed deployment.

---

### Likelihood Explanation

Any transaction that attaches a type script to an output without also using that same script as a lock script (the common case for UDT/NFT type scripts) will trigger this path during a reorg on PostgreSQL. Reorgs are a normal part of chain operation. No special attacker capability is required — a standard transaction sender submitting a type-script-bearing transaction is sufficient.

---

### Recommendation

Change line 252 from `row_lock.try_get::<bool, _>(0)` to `row_type.try_get::<bool, _>(0)`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {   // was: row_lock
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

---

### Proof of Concept

1. Deploy a PostgreSQL-backed CKB rich indexer node.
2. Submit a transaction whose output carries a type script `S` but whose lock script is a different script (the common pattern for UDT cells).
3. Mine the block containing that transaction.
4. Trigger a reorg that removes that block (e.g., by mining a longer competing chain).
5. During removal, `script_exists_in_output` is called for script `S`.
   - `row_lock` query returns `EXISTS = false` (S is not a lock script).
   - The early-return guard is not triggered.
   - `row_type` query returns `EXISTS = true` (S is a type script).
   - The final `match` reads `row_lock.try_get::<bool, _>(0)` → `Ok(false)`.
   - Function returns `Ok(false)` — incorrect.
6. The caller deletes script `S` from the script table despite it still being referenced.
7. Subsequent `get_cells` or `get_transactions` RPC calls for cells using script `S` return empty or corrupt results. [2](#0-1)

### Citations

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
