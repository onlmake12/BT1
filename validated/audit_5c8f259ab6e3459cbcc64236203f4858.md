The bug at line 252 is real and confirmed. Let me verify the exact behavior.

### Title
Copy-Paste Bug in `script_exists_in_output` Causes Type Script Deletion on PostgreSQL During Rollback — (`util/rich-indexer/src/indexer/remove.rs`)

---

### Summary

A copy-paste error at line 252 of `script_exists_in_output` causes the function to re-examine `row_lock` instead of `row_type` in the final `match` arm. On PostgreSQL, this makes the function always return `false` for any script that is only referenced as a `type_script_id` (not as a `lock_script_id`) in surviving outputs. During a rollback triggered by a reorg, this causes still-referenced type scripts to be incorrectly deleted from the `script` table, corrupting the rich indexer's relational state.

---

### Finding Description

In `script_exists_in_output`: [1](#0-0) 

The function queries two rows:
- `row_lock`: `SELECT EXISTS (... WHERE lock_script_id = $1)`
- `row_type`: `SELECT EXISTS (... WHERE type_script_id = $1)`

The first `match` block (lines 223–235) correctly short-circuits with `Ok(true)` if the script is found as a lock script. If not, `row_type` is fetched. But the **second** `match` at line 252 reads `row_lock.try_get::<bool, _>(0)` instead of `row_type.try_get::<bool, _>(0)`.

**On PostgreSQL**, `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL `EXISTS` returns `BOOLEAN`). Since the first match already confirmed `row_lock` is `false` (otherwise we would have returned early), the second match also evaluates `row_lock` as `false` and returns `Ok(false)` — **without ever inspecting `row_type`**.

**On SQLite**, `row_lock.try_get::<bool, _>(0)` fails (SQLite returns `BIGINT`), so the `Err(_)` arm falls through to `Ok(row_type.get::<i64, _>(0) == 1)`, which is correct. SQLite is unaffected.

The caller `rollback_block` uses this function to decide which scripts to delete: [2](#0-1) 

Outputs are deleted first (line 25), then `script_exists_in_output` is called to check if any remaining output still references each script. On PostgreSQL, the type-script check is silently skipped, so a type script shared by surviving outputs is incorrectly added to `script_id_list_to_remove` and deleted.

---

### Impact Explanation

After a reorg on a PostgreSQL-backed rich indexer node:

1. Type scripts that are still referenced by surviving outputs are deleted from the `script` table.
2. Subsequent `get_cells` queries JOIN `output` against `script` — the deleted rows produce `NULL` type script fields in results.
3. Applications (wallets, dApps) querying by type script will either miss cells entirely or receive malformed cell data.
4. The corruption is permanent until the indexer is rebuilt from scratch.

The `get_cells` RPC is a documented production API: [3](#0-2) 

PostgreSQL is a documented, supported production configuration: [4](#0-3) 

---

### Likelihood Explanation

Reorgs are a normal, unprivileged network event. Any peer can relay a valid competing chain of sufficient work to trigger a rollback. No special privileges, leaked keys, or majority hashpower are required. The only precondition is that the operator uses the PostgreSQL backend (not the default SQLite). PostgreSQL is the recommended backend for production deployments requiring performance and secondary development.

---

### Recommendation

Change line 252 from:

```rust
match row_lock.try_get::<bool, _>(0) {
```

to:

```rust
match row_type.try_get::<bool, _>(0) {
``` [5](#0-4) 

---

### Proof of Concept

1. Start a CKB node with `--rich-indexer` and PostgreSQL backend.
2. Append a block containing two outputs sharing the same `type_script` but different `lock_scripts` (e.g., output1: lock=L1, type=T1; output2: lock=L2, type=T1).
3. Append a second block containing one output referencing the same type script (output3: lock=L3, type=T1).
4. Trigger a rollback of block 2 (simulate a reorg).
5. Query the `script` table: T1's row will be absent even though output1 and output2 in block 1 still reference it.
6. Call `get_cells` filtering by T1 — results will have NULL type script data or return no results.

The root cause is confirmed at: [6](#0-5)

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

**File:** rpc/src/module/rich_indexer.rs (L65-72)
```rust
    #[rpc(name = "get_cells")]
    async fn get_cells(
        &self,
        search_key: IndexerSearchKey,
        order: IndexerOrder,
        limit: Uint32,
        after: Option<JsonBytes>,
    ) -> Result<IndexerPagination<IndexerCell>>;
```

**File:** util/rich-indexer/README.md (L59-102)
```markdown
## Enabling Rich Indexer with PostgreSQL

To enable PostgreSQL, you must first set up a functional PostgreSQL service on your own. Please refer to [Server Administration](https://www.postgresql.org/docs/16/admin.html) for guidance. It is recommended to install version 16 or above.

For hardware with 4 cores and 8 GB of RAM, it is recommended to make the following two configuration parameter adjustments in PostgreSQL to achieve optimal query performance.

postgresql.conf:

```conf
#------------------------------------------------------------------------------
# RESOURCE USAGE (except WAL)
#------------------------------------------------------------------------------

# - Memory -

shared_buffers = 2GB                    # min 128kB
```

```conf
#------------------------------------------------------------------------------
# QUERY TUNING
#------------------------------------------------------------------------------

# - Other Planner Options -

jit = off                               # allow JIT compilation
```

Next, configure the PostgreSQL connection parameters for the CKB node.

ckb.toml:

```toml
# CKB rich-indexer has its unique configuration.
[indexer_v2.rich_indexer]
# By default, it uses an embedded SQLite database.
# Alternatively, you can set up a PostgreSQL database service and provide the connection parameters.
db_type = "postgres"
db_name = "ckb-rich-indexer"
db_host = "127.0.0.1"
db_port = 5432
db_user = "postgres"
db_password = "123456"
```
```
