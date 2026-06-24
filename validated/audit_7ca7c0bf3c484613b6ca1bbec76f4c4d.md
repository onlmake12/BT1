Audit Report

## Title
Unconditional `.unwrap()` on Block Tip Query Panics Under Concurrent Rollback Race — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

## Summary

`get_cells_capacity` opens a Postgres transaction via `pool.begin()` at the default READ COMMITTED isolation level, executes a capacity aggregate query, and then unconditionally `.unwrap()`s the result of a second block-tip query within the same transaction. Under READ COMMITTED, each statement takes a fresh committed snapshot. A concurrent `rollback_block` that commits between the two queries can leave the block table empty at the second snapshot while the first snapshot already returned `Some(N)` capacity, causing the `.unwrap()` at line 215 to panic and abort the RPC handler task.

## Finding Description

The two-query sequence in `get_cells_capacity` is confirmed in the actual source: [1](#0-0) 

The transaction is started with `self.store.transaction()`, which calls `pool.begin()` with no explicit isolation level: [2](#0-1) 

For Postgres, `pool.begin()` defaults to READ COMMITTED, meaning each statement within the transaction takes a fresh snapshot of committed data. The capacity query at lines 187–195 can return `Some(N)` (live cells exist at snapshot T1), and execution falls through past the early-return guard. The block-tip query then executes: [3](#0-2) 

The `.unwrap()` at line 215 is unconditional. If a concurrent `rollback_block` transaction commits between the two queries, the block table is empty at snapshot T2, `fetch_optional` returns `Ok(None)`, and `.unwrap()` panics.

`rollback_block` deletes the block row as part of its own committed transaction: [4](#0-3) 

The `AsyncRichIndexer::rollback()` wraps this in its own `pool.begin()` / `commit()` cycle, making it a fully independent committed transaction: [5](#0-4) 

The indexer sync loop calls `rollback()` during reorgs: [6](#0-5) 

No existing guard prevents the panic: the only guard in the function is the `None => return Ok(None)` check at line 192–194, which is bypassed once `Some(capacity)` is matched. There is no `?`-propagation or `ok_or` on the `.unwrap()` at line 215.

## Impact Explanation

A panic in the async Tokio task serving the `get_cells_capacity` RPC request aborts that task. The Tokio runtime catches the panic and drops the task; the uncommitted `tx` is automatically rolled back by sqlx on drop, so there is no transaction leak. The node process itself continues running. The concrete impact is a crash of the local RPC API handler for `get_cells_capacity` on any node running the rich-indexer with a Postgres backend during a reorg. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**.

## Likelihood Explanation

- Postgres is a supported production backend; no isolation level override exists anywhere in the rich-indexer store code.
- Reorgs (and thus `rollback()` calls) are normal production events on CKB.
- READ COMMITTED is the Postgres default; `pool.begin()` is called without any `SET TRANSACTION ISOLATION LEVEL` override.
- The race window is narrow (between two sequential async `.await` points) but repeatable: an attacker calling `get_cells_capacity` in a tight loop during any reorg will eventually hit it.
- No authentication is required to call `get_cells_capacity` via the JSON-RPC interface.

## Recommendation

Replace the unconditional `.unwrap()` at line 215 with graceful error propagation:

```rust
.ok_or_else(|| Error::DB("block tip not found".to_string()))?;
```

Or return `Ok(None)` to match the semantics of "no indexed tip available." Additionally, consider upgrading the transaction isolation level to `REPEATABLE READ` so both queries see the same committed snapshot, eliminating the race entirely.

## Proof of Concept

Race sequence:
1. Indexer has exactly block 0 indexed with live cells matching the `search_key`.
2. RPC caller sends `get_cells_capacity` with a broad `search_key`.
3. Capacity query executes inside `tx` at snapshot T1 → finds live cells → returns `Some(N)` → execution continues past line 194.
4. Concurrent `rollback_block` transaction commits: deletes outputs from block 0, deletes the block 0 row from the `block` table.
5. Block-tip query executes inside `tx` at snapshot T2 (READ COMMITTED fresh snapshot) → `block` table is empty → `fetch_optional` returns `Ok(None)`.
6. `.unwrap()` at line 215 panics, aborting the Tokio task.

Minimal test plan: write an integration test against a real Postgres instance that (a) appends block 0 with outputs, (b) spawns a task calling `get_cells_capacity` in a loop, and (c) concurrently calls `rollback()`. The panic will be observed within a small number of iterations.

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L180-195)
```rust
        let mut tx = self
            .store
            .transaction()
            .await
            .map_err(|err| Error::DB(err.to_string()))?;

        // fetch
        let capacity = query
            .fetch_optional(&mut *tx)
            .await
            .map_err(|err| Error::DB(err.to_string()))?
            .and_then(|row| row.try_get::<i64, _>("total_capacity").ok());
        let capacity = match capacity {
            Some(capacity) => capacity as u64,
            None => return Ok(None),
        };
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L197-215)
```rust
        let (block_hash, block_number) = SQLXPool::new_query(
            r#"
                SELECT block_hash, block_number FROM block
                ORDER BY id DESC
                LIMIT 1
                "#,
        )
        .fetch_optional(&mut *tx)
        .await
        .map(|res| {
            res.map(|row| {
                (
                    bytes_to_h256(row.get("block_hash")),
                    row.get::<i64, _>("block_number") as u64,
                )
            })
        })
        .map_err(|err| Error::DB(err.to_string()))?
        .unwrap();
```

**File:** util/rich-indexer/src/store.rs (L175-178)
```rust
    pub async fn transaction(&self) -> Result<Transaction<'_, Any>> {
        let pool = self.get_pool()?;
        pool.begin().await.map_err(Into::into)
    }
```

**File:** util/rich-indexer/src/indexer/remove.rs (L41-43)
```rust
    // remove block and block associations
    let uncle_id_list = query_uncle_id_list_by_block_id(block_id, tx).await?;
    remove_batch_by_blobs("block", "id", &[block_id], tx).await?;
```

**File:** util/rich-indexer/src/indexer/mod.rs (L180-190)
```rust
    pub(crate) async fn rollback(&self) -> Result<(), Error> {
        let mut tx = self
            .store
            .transaction()
            .await
            .map_err(|err| Error::DB(err.to_string()))?;

        rollback_block(&mut tx).await?;

        tx.commit().await.map_err(|err| Error::DB(err.to_string()))
    }
```

**File:** util/indexer-sync/src/lib.rs (L163-170)
```rust
                            } else {
                                info!(
                                    "{} rollback {}, {}",
                                    indexer.get_identity(),
                                    tip_number,
                                    tip_hash
                                );
                                indexer.rollback().expect("rollback block should be OK");
```
