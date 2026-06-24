Audit Report

## Title
Unbounded Aggregation in `get_cells_capacity` Enables DoS on Indexer-Enabled Nodes — (File: `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`, `util/indexer/src/service.rs`)

## Summary

The `get_cells_capacity` RPC endpoint accepts a `search_key` with no `limit` parameter and no server-side row cap, while the sibling endpoints `get_cells` and `get_transactions` both enforce a caller-supplied limit validated against `self.request_limit`. In the rich indexer, this results in an unbounded SQL `SUM` aggregation over the entire `output` table. Any unprivileged caller can issue a single HTTP POST with a broad prefix `search_key` to force a full-table scan, exhausting I/O and CPU and rendering the RPC server unresponsive for the duration.

## Finding Description

**Rich indexer — no limit check in `get_cells_capacity`:**

`get_cells` and `get_transactions` both validate the caller-supplied limit against `self.request_limit`: [1](#0-0) [2](#0-1) 

`get_cells_capacity` has no equivalent check and accepts only a `search_key`: [3](#0-2) 

It issues a raw SQL `SUM` aggregation with no `LIMIT` clause, scanning every matching live cell: [4](#0-3) 

The query runs inside a database transaction that is held open for the full duration: [5](#0-4) 

**Basic (RocksDB) indexer — `TimeoutIterator` is the only guard:**

The basic indexer wraps the iterator in a `TimeoutIterator` keyed on `self.timeout_limit`, a service-level constant, not a per-request row cap: [6](#0-5) 

The iterator then scans all keys matching the prefix with no row bound: [7](#0-6) 

**RPC trait confirms no `limit` parameter:** [8](#0-7) 

## Impact Explanation

Sustained concurrent calls to `get_cells_capacity` with a broad prefix `search_key` (e.g., empty `args` in prefix mode) force the rich indexer to execute an unbounded `SUM` aggregation holding a DB transaction open, and force the basic indexer to iterate all matching RocksDB keys. On a mature mainnet node with tens of millions of live cells, each call can saturate I/O and CPU for seconds. Concurrent or rapid repeated calls can exhaust the database connection pool and render the RPC server unresponsive, effectively crashing the node's RPC layer. This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

No authentication, fee, or proof-of-work is required. Any unprivileged caller with HTTP access to the RPC port can trigger this. Operators who expose the `Indexer` or `RichIndexer` module for wallet/dapp support (a common production configuration) cannot selectively disable `get_cells_capacity` without disabling the entire module. The attack cost is a single HTTP POST per invocation and is trivially repeatable.

## Recommendation

1. Add a server-side `request_limit` check to `get_cells_capacity` in both the rich indexer (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`) and the basic indexer (`util/indexer/src/service.rs`), analogous to the check already present in `get_cells` and `get_transactions`.
2. Enforce a mandatory maximum scan-row cap (e.g., via a `LIMIT` clause in the SQL query or a hard row counter in the RocksDB iterator path) to bound the work per call regardless of caller input.
3. Consider requiring a `block_range` filter or a minimum script specificity to prevent empty-prefix scans.

## Proof of Concept

Send the following request repeatedly to a node with `Indexer` or `RichIndexer` enabled:

```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "get_cells_capacity",
  "params": [
    {
      "script": {
        "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
        "hash_type": "type",
        "args": "0x"
      },
      "script_type": "lock",
      "script_search_mode": "prefix"
    }
  ]
}
```

The empty `args` with `prefix` mode matches every live cell indexed under any lock script with the given `code_hash`, maximizing scan width. Sending this concurrently (e.g., 10–20 parallel connections) will exhaust the database connection pool and block all other RPC requests for the duration of each aggregation.

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L25-34)
```rust
        let limit = limit.value();
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L23-32)
```rust
        let limit = limit.value();
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L13-16)
```rust
    pub async fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>, Error> {
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L26-27)
```rust
        let mut query_builder = SqlBuilder::select_from("output");
        query_builder.field("CAST(SUM(output.capacity) AS BIGINT) AS total_capacity");
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L180-191)
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
```

**File:** util/indexer/src/service.rs (L718-720)
```rust
        let mode = IteratorMode::From(from_key.as_ref(), direction);
        let snapshot = self.store.inner().snapshot();
        let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
```

**File:** util/indexer/src/service.rs (L726-729)
```rust
        let capacity: u64 = iter
            .by_ref()
            .take_while(|(key, _value)| key.starts_with(&prefix))
            .filter_map(|(key, value)| {
```

**File:** rpc/src/module/indexer.rs (L879-883)
```rust
    #[rpc(name = "get_cells_capacity")]
    fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>>;
```
