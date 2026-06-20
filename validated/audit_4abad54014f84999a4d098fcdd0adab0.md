The code is present and readable. Let me analyze the exact bug at line 252.

The bug is real and precisely located. Here is the full analysis:

---

### Title
PostgreSQL-Only Script Retention Failure in `script_exists_in_output` Silently Corrupts Type-Script Index After Reorg — (`util/rich-indexer/src/indexer/remove.rs`)

---

### Summary

A copy-paste error at line 252 of `script_exists_in_output` causes the function to re-read `row_lock` (the lock-script EXISTS result) instead of `row_type` (the type-script EXISTS result) when determining whether a script is still referenced. On PostgreSQL, `row_lock.try_get::<bool, _>(0)` succeeds (PostgreSQL returns `BOOLEAN` for `EXISTS`), so the `Ok(r)` arm always fires and returns the lock-query result — silently discarding the type-query result. On SQLite, `try_get::<bool, _>` fails (SQLite returns `BIGINT`), so the `Err(_)` arm fires and correctly reads `row_type`. The bug is therefore PostgreSQL-exclusive and invisible to the existing test suite, which only exercises SQLite.

---

### Finding Description

**Root cause — line 252:**

```rust
// After fetching row_type for the type_script EXISTS query...
match row_lock.try_get::<bool, _>(0) {   // ← should be row_type
    Ok(r) => Ok(r),                       // returns lock result, not type result
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
``` [1](#0-0) 

The correct variable at line 252 is `row_type`, not `row_lock`. The first half of the function (lines 223–235) correctly handles the early-return case when the lock query is `true`. The second half (lines 252–256) is supposed to return the type-query result, but instead returns the already-evaluated lock-query result a second time. [2](#0-1) 

**Trigger path in `rollback_block`:**

1. `rollback_block` collects `output_lock_type_list` for the tip block **before** deleting its outputs.
2. It deletes the tip block's outputs (line 25).
3. For each `(_, lock_script_id, type_script_id)` in that list, it calls `script_exists_in_output` to decide whether to delete the script row (lines 29–38). [3](#0-2) 

**Concrete failure scenario (PostgreSQL):**

| Step | State |
|---|---|
| Block N appended | Script S stored; S is `type_script_id` of output O_N |
| Block N+1 appended | S reused as `type_script_id` of output O_N1 |
| Block N+1 rolled back | O_N1 deleted; `script_exists_in_output(S)` called |
| Lock query | `SELECT EXISTS(... WHERE lock_script_id = S)` → `false` |
| PG: `row_lock.try_get::<bool,_>(0)` | Succeeds → no early return (correct) |
| Type query | `SELECT EXISTS(... WHERE type_script_id = S)` → `true` (O_N still exists) |
| Line 252: `match row_lock.try_get::<bool,_>(0)` | Succeeds → returns `row_lock` value = `false` ← **BUG** |
| Result | `script_exists_in_output` returns `false`; S added to removal list |
| S deleted from `script` table | All subsequent `type_script_id`-based queries for S return empty |

---

### Impact Explanation

After any reorg on a PostgreSQL-backed rich indexer node, every script that is referenced **only** as a `type_script_id` in surviving outputs (never as a `lock_script_id`) is permanently deleted from the `script` table. All RPC queries that filter by type script (`get_cells`, `get_transactions` with `script_type: type`) return empty results for those scripts. This is silent data corruption: the indexer continues to operate without error, but its query results are wrong.

---

### Likelihood Explanation

Reorgs are a routine, unprivileged event on any live CKB network. Any block relayer can submit a valid competing chain tip that causes the node to roll back one or more blocks. No special privileges, keys, or majority hashpower are required to trigger the rollback path. The PostgreSQL backend is the recommended production deployment for the rich indexer. The existing test suite uses only SQLite in-memory databases, so this divergence has never been caught by CI. [4](#0-3) 

---

### Recommendation

Change line 252 from `row_lock` to `row_type`:

```rust
// pg type is BOOLEAN
match row_type.try_get::<bool, _>(0) {
    Ok(r) => Ok(r),
    // sqlite type is BIGINT
    Err(_) => Ok(row_type.get::<i64, _>(0) == 1),
}
```

Additionally, add a PostgreSQL integration test that:
1. Appends two blocks where a script S appears only as `type_script_id` in block N and block N+1.
2. Rolls back block N+1.
3. Asserts the `script` table still contains S.
4. Asserts `get_cells` with `script_type: type` for S returns the block N output.

---

### Proof of Concept

```
# Differential test (pseudocode)
for backend in [sqlite, postgresql]:
    append block_N  (output O_N: lock=L, type=S)
    append block_N1 (output O_N1: lock=L2, type=S)
    rollback block_N1
    count = SELECT COUNT(*) FROM script WHERE id = <id of S>
    assert count == 1, f"{backend}: S must survive rollback"

# On PostgreSQL, count == 0 (BUG); on SQLite, count == 1 (correct)
```

The divergence is directly observable by comparing `storage.fetch_count("script")` across backends after an identical append/rollback sequence — exactly the pattern used in the existing `test_rollback_block_9` test, but run against PostgreSQL. [5](#0-4)

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

**File:** util/rich-indexer/src/tests/rollback.rs (L6-8)
```rust
async fn test_rollback_block_0() {
    let storage = connect_sqlite(MEMORY_DB).await;
    let indexer = AsyncRichIndexer::new(
```

**File:** util/rich-indexer/src/tests/rollback.rs (L61-143)
```rust
#[test]
async fn test_rollback_block_9() {
    let storage = connect_sqlite(MEMORY_DB).await;
    let indexer = AsyncRichIndexer::new(
        storage.clone(),
        None,
        CustomFilters::new(
            Some("block.header.number.to_uint() >= \"0x0\".to_uint()"),
            None,
        ),
    );
    insert_blocks(storage.clone()).await;

    assert_eq!(15, storage.fetch_count("block").await.unwrap()); // 10 blocks, 5 uncles
    assert_eq!(11, storage.fetch_count("ckb_transaction").await.unwrap());
    assert_eq!(12, storage.fetch_count("output").await.unwrap());
    assert_eq!(1, storage.fetch_count("input").await.unwrap());
    assert_eq!(9, storage.fetch_count("script").await.unwrap());
    assert_eq!(
        0,
        storage
            .fetch_count("block_association_proposal")
            .await
            .unwrap()
    );
    assert_eq!(
        5,
        storage
            .fetch_count("block_association_uncle")
            .await
            .unwrap()
    );
    assert_eq!(
        0,
        storage
            .fetch_count("tx_association_header_dep")
            .await
            .unwrap()
    );
    assert_eq!(
        2,
        storage
            .fetch_count("tx_association_cell_dep")
            .await
            .unwrap()
    );

    indexer.rollback().await.unwrap();

    assert_eq!(12, storage.fetch_count("block").await.unwrap()); // 9 blocks, 3 uncles
    assert_eq!(10, storage.fetch_count("ckb_transaction").await.unwrap());
    assert_eq!(12, storage.fetch_count("output").await.unwrap());
    assert_eq!(1, storage.fetch_count("input").await.unwrap());
    assert_eq!(9, storage.fetch_count("script").await.unwrap());
    assert_eq!(
        0,
        storage
            .fetch_count("block_association_proposal")
            .await
            .unwrap()
    );
    assert_eq!(
        3,
        storage
            .fetch_count("block_association_uncle")
            .await
            .unwrap()
    );
    assert_eq!(
        0,
        storage
            .fetch_count("tx_association_header_dep")
            .await
            .unwrap()
    );
    assert_eq!(
        2,
        storage
            .fetch_count("tx_association_cell_dep")
            .await
            .unwrap()
    );
}
```
