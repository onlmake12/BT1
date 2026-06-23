The code is clear. Let me verify the transaction isolation level and the exact panic path.

### Title
Unconditional `.unwrap()` on Block Tip Query Panics Under Concurrent Rollback Race — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

### Summary

`get_cells_capacity` opens a Postgres transaction at the default READ COMMITTED isolation level, runs a capacity aggregate query, and then unconditionally `.unwrap()`s the result of a second block-tip query within the same transaction. Under READ COMMITTED, each statement sees a fresh committed snapshot. A concurrent `rollback_block` that commits between the two queries produces a state where the capacity query observes live cells (returning `Some(N)`) while the block-tip query observes an empty `block` table (returning `None`), causing the `.unwrap()` to panic.

### Finding Description

The two-query sequence in `get_cells_capacity`: [1](#0-0) 

starts a transaction with no explicit isolation level: [2](#0-1) 

`pool.begin()` uses the database default — **READ COMMITTED** for Postgres — meaning each statement within the transaction takes a fresh snapshot of committed data. The capacity query at lines 187–195 can return `Some(N)` (live cells exist at snapshot T1). Execution then falls through to the block-tip query: [3](#0-2) 

The `.unwrap()` at line 215 is unconditional. If a concurrent `rollback_block` transaction commits between the two queries, the block table is empty at snapshot T2, `fetch_optional` returns `None`, and `.unwrap()` panics.

`rollback_block` deletes the block row as part of its own committed transaction: [4](#0-3) 

The indexer sync loop calls `rollback()` during reorgs: [5](#0-4) 

### Impact Explanation

A panic in the async RPC handler aborts the Tokio task serving that request. Depending on how the jsonrpc server wraps handlers, this may propagate to crash the process or silently kill the handler task while leaking the uncommitted DB transaction. Either way, the invariant that RPC handlers must never panic on valid concurrent DB states is violated. An attacker can repeatedly trigger this during any reorg window.

### Likelihood Explanation

- Postgres backend is a supported production configuration.
- Reorgs (and thus `rollback()` calls) are normal production events.
- READ COMMITTED is the Postgres default; no isolation level override exists anywhere in the rich-indexer store code.
- The race window is narrow but repeatable: an attacker calling `get_cells_capacity` in a tight loop during a reorg will eventually hit it.
- No authentication is required to call `get_cells_capacity` via RPC.

### Recommendation

Replace the unconditional `.unwrap()` at line 215 with graceful handling:

```rust
.ok_or_else(|| Error::DB("block tip not found".to_string()))?;
```

Or return `Ok(None)` to match the semantics of "no indexed tip". Additionally, upgrade the transaction isolation level to `REPEATABLE READ` so both queries see the same snapshot, eliminating the race entirely.

### Proof of Concept

Race sequence:
1. Indexer has block 0 indexed with live cells.
2. RPC caller sends `get_cells_capacity` with a broad prefix `search_key`.
3. Capacity query executes inside `tx` at snapshot T1 → finds live cells → returns `Some(N)` → execution continues past line 194.
4. Concurrent `rollback_block` transaction commits, deleting the block row from the `block` table.
5. Block-tip query executes inside `tx` at snapshot T2 (READ COMMITTED fresh snapshot) → `block` table is empty → `fetch_optional` returns `None`.
6. `.unwrap()` at line 215 panics. [6](#0-5)

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

**File:** util/rich-indexer/src/indexer/remove.rs (L43-43)
```rust
    remove_batch_by_blobs("block", "id", &[block_id], tx).await?;
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
