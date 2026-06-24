Audit Report

## Title
Unconditional `.unwrap()` on Block Tip Query Panics Under Concurrent Rollback Race — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

## Summary
`get_cells_capacity` opens a Postgres transaction at READ COMMITTED isolation (the default, with no override) and unconditionally `.unwrap()`s the result of a `fetch_optional` block-tip query at line 215. Under READ COMMITTED, each statement takes a fresh committed snapshot. A concurrent `rollback_block` that commits between the capacity query and the block-tip query can leave the block table empty while the capacity query already returned `Some(N)`, causing `.unwrap()` to panic and aborting the RPC handler task.

## Finding Description
`self.store.transaction()` at `get_cells_capacity.rs` line 180–184 calls `pool.begin()` at `store.rs` lines 175–178 with no explicit isolation level, defaulting to READ COMMITTED on Postgres. The capacity aggregate query at lines 187–195 runs at snapshot T1 and returns `Some(N)` when live cells exist, allowing execution to continue past the early-return at line 194. The block-tip query at lines 197–215 then runs at snapshot T2. In `mod.rs` lines 180–190, `rollback()` opens its own independent transaction and commits it atomically; `rollback_block` in `remove.rs` line 43 deletes the block row as part of that commit. If this commit lands between T1 and T2 and the block table becomes empty (e.g., exactly one block was indexed), `fetch_optional` returns `None` and `.unwrap()` at line 215 panics. No guard exists between the two queries to handle this state. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

## Impact Explanation
A panic in the async RPC handler aborts the Tokio task for that request, producing an unhandled crash for the caller. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**. The impact does not extend to crashing the full node process because Tokio catches task panics without `panic = "abort"`.

## Likelihood Explanation
- Postgres is a supported production backend; READ COMMITTED is its default and no override exists anywhere in the rich-indexer store code.
- Reorgs (and thus `rollback()` calls) are normal production events triggered by the sync loop.
- The race requires the block table to be empty after rollback, which concretely occurs when exactly one block is indexed at the time of rollback — a real but narrow window (e.g., at indexer startup during an early reorg).
- No authentication is required to call `get_cells_capacity` via RPC; any external caller can repeatedly probe during a known reorg window.

## Recommendation
Replace the unconditional `.unwrap()` at line 215 with graceful error handling:
```rust
.ok_or_else(|| Error::DB("block tip not found".to_string()))?;
```
Or return `Ok(None)` to match the semantics of "no indexed tip available." Additionally, set the transaction isolation level to `REPEATABLE READ` so both queries observe the same committed snapshot, eliminating the race entirely.

## Proof of Concept
1. Start a CKB node with the rich-indexer configured against Postgres.
2. Allow exactly one block (block 0) to be indexed.
3. Trigger a reorg so the sync loop calls `indexer.rollback()`.
4. Concurrently send `get_cells_capacity` with a broad `search_key` that matches block 0's cells.
5. If the capacity query executes before the rollback commits and the block-tip query executes after, `fetch_optional` returns `None` and `.unwrap()` at line 215 panics, aborting the handler task.
6. Confirm by observing a Tokio task panic in the node logs with no JSON-RPC response returned to the caller.

### Citations

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

**File:** util/rich-indexer/src/indexer/remove.rs (L41-43)
```rust
    // remove block and block associations
    let uncle_id_list = query_uncle_id_list_by_block_id(block_id, tx).await?;
    remove_batch_by_blobs("block", "id", &[block_id], tx).await?;
```
