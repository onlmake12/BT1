The bug is confirmed in the code. Let me analyze it precisely.

At line 252 in `script_exists_in_output`, the second `match` block reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`: [1](#0-0) 

The function fetches `row_type` (the EXISTS result for `type_script_id`) at lines 237–249, but then at line 252 re-reads `row_lock` (the EXISTS result for `lock_script_id`) instead of `row_type`. On **PostgreSQL**, `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN` for `EXISTS`), so it returns `row_lock`'s value — which is already known to be `false` (the early-return at line 225–227 would have fired otherwise). The `row_type` result is silently discarded. On **SQLite**, `try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err(_)` branch at line 255 correctly reads `row_type.get::<i64, _>(0)` — the bug is PostgreSQL-only.

The consequence in `rollback_block`: [2](#0-1) 

For every output in the rolled-back block, if its script is referenced only via `type_script_id` (not `lock_script_id`) in any surviving output, `script_exists_in_output` returns `false` on PostgreSQL, and the script row is pushed into `script_id_list_to_remove` and deleted.

After deletion, `get_cells` and `get_transactions` RPC handlers join on `type_script_id`: [3](#0-2) 

With the script row gone, those joins return zero rows — the surviving cells become invisible to any wallet or DApp querying through the rich-indexer.

---

### Title
PostgreSQL-only copy-paste bug in `script_exists_in_output` causes incorrect script deletion during rollback — (`util/rich-indexer/src/indexer/remove.rs`)

### Summary
In `script_exists_in_output`, the final `match` at line 252 re-reads `row_lock` instead of `row_type`. On PostgreSQL this silently ignores whether the script is still referenced as a `type_script_id`, causing `rollback_block` to delete live script rows and making all cells referencing those scripts invisible via RPC.

### Finding Description
`script_exists_in_output` performs two SQL `EXISTS` queries: one for `lock_script_id` and one for `type_script_id`. If the first returns false, the function should return the result of the second. Instead, at line 252, it re-evaluates `row_lock` (the first query's row) a second time. On PostgreSQL, `try_get::<bool, _>(0)` succeeds on `row_lock`, returning `false` again — the `row_type` result is never consulted. The function therefore always returns `false` for any script that is not a `lock_script_id` in surviving outputs, regardless of whether it is still a `type_script_id`.

### Impact Explanation
Any chain reorganization (a normal P2P event) that rolls back a block containing an output whose script is shared with a surviving output only via `type_script_id` will cause that script row to be deleted from the `script` table. All subsequent `get_cells`/`get_transactions` RPC calls filtering by that type script return empty results. Wallets and DApps relying on the rich-indexer cannot locate those cells. The only recovery is a full re-index. Affects all PostgreSQL-backed rich-indexer deployments; SQLite is unaffected due to the type-mismatch fallback path.

### Likelihood Explanation
Chain reorganizations are routine on mainnet (any peer can present a longer valid chain). No special privileges are required. The precondition — a type script shared between a rolled-back output and a surviving output — is common (e.g., any token/NFT contract script used across multiple cells). The bug is deterministic and reproducible in a local test.

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

### Proof of Concept
1. Start a PostgreSQL-backed rich-indexer node.
2. Append `block0` containing an output with a unique `type_script` S (and any `lock_script` L0).
3. Append `block1` containing an output also using S as `type_script` (and any `lock_script` L1 ≠ L0).
4. Roll back `block1`.
5. Assert: `SELECT * FROM script WHERE id = <S_id>` returns a row (it will be empty — bug confirmed).
6. Assert: `get_cells` RPC for script S returns `block0`'s output (it will return nothing — bug confirmed).

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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L84-91)
```rust
        match search_key.script_type {
            IndexerScriptType::Lock => {
                query_builder.on("output.lock_script_id = query_script.id");
            }
            IndexerScriptType::Type => {
                query_builder.on("output.type_script_id = query_script.id");
            }
        }
```
