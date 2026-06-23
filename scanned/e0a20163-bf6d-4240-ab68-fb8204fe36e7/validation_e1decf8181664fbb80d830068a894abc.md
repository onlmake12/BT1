### Title
Unbounded Cell Iteration in `get_cells_capacity` Enables RPC-Triggered Resource Exhaustion — (`File: util/indexer/src/service.rs`)

---

### Summary

The `get_cells_capacity` RPC endpoint in the CKB indexer iterates over every matching live cell in the RocksDB index without any count limit. Unlike `get_cells` and `get_transactions`, which accept a caller-supplied `limit: Uint32` parameter, `get_cells_capacity` accepts only a `search_key` and scans the entire matching collection. The sole protection is a configurable wall-clock timeout (default 10 seconds). An unprivileged RPC caller can craft a search key that matches a large number of cells and issue repeated concurrent requests, causing sustained CPU and I/O exhaustion on the node's indexer service.

---

### Finding Description

`get_cells` and `get_transactions` both require a `limit: Uint32` argument and terminate iteration after that many results: [1](#0-0) 

`get_cells_capacity` takes no `limit` parameter at all: [2](#0-1) 

Inside `IndexerService::get_cells_capacity`, the implementation builds a `TimeoutIterator` and then calls `.take_while(...).filter_map(...)` with no `.take(n)` count cap — it scans every key in the RocksDB prefix range until the prefix no longer matches or the 10-second wall-clock timeout fires: [3](#0-2) 

The `request_limit` field is stored on `IndexerService` and is used to cap user-supplied `limit` values in `get_cells`/`get_transactions`, but it is never consulted inside `get_cells_capacity`: [4](#0-3) 

The `TimeoutIterator` only stops iteration after the elapsed wall-clock time exceeds `timeout_limit`; it does not bound the number of RocksDB key reads or the CPU work done per request: [5](#0-4) 

The rich-indexer variant (`AsyncRichIndexerHandle::get_cells_capacity`) delegates to a SQL `SUM(capacity)` aggregate query with no row limit, so the database engine must scan all matching rows: [6](#0-5) 

---

### Impact Explanation

A single `get_cells_capacity` call with a broad prefix (e.g., the secp256k1 lock `code_hash`, which matches every standard wallet cell on mainnet) forces the indexer to read and deserialize every matching live cell from RocksDB for up to 10 seconds. Sending a small number of concurrent requests saturates the indexer's I/O and CPU for the full timeout window. This degrades or blocks all other indexer RPC responses (`get_cells`, `get_transactions`, `get_cells_capacity`) for legitimate callers during the attack window. The node's chain-processing and P2P functions are separate, so consensus is not directly affected, but the indexer service becomes unavailable.

---

### Likelihood Explanation

The indexer RPC is exposed by default on any CKB node that enables the indexer module. No authentication is required. The attack requires only a single JSON-RPC call with a well-known `code_hash` (publicly documented in the CKB genesis script list). The secp256k1 lock script is used by every standard CKB wallet, so a prefix search on its `code_hash` matches a very large fraction of all live cells on mainnet. The attack is trivially repeatable and requires no special knowledge beyond the public RPC documentation.

---

### Recommendation

1. Add a `limit: Option<Uint32>` parameter to `get_cells_capacity` (both the basic indexer and rich-indexer variants) and enforce `request_limit` as the maximum, consistent with `get_cells` and `get_transactions`.
2. Alternatively, enforce a hard internal cap (e.g., `request_limit`) on the number of cells scanned inside `get_cells_capacity` regardless of caller input, returning an error if the cap is reached.
3. The `TimeoutIterator` should remain as a secondary defense but must not be the only bound.

---

### Proof of Concept

Send the following JSON-RPC request to a CKB node with the indexer enabled, using the secp256k1 lock `code_hash` (which matches all standard wallet cells):

```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "get_cells_capacity",
  "params": [{
    "script": {
      "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
      "hash_type": "type",
      "args": "0x"
    },
    "script_type": "lock",
    "script_search_mode": "prefix"
  }]
}
```

With `args: "0x"` (empty prefix), the `take_while` loop in `get_cells_capacity` will iterate every cell whose lock script begins with the secp256k1 `code_hash` — potentially millions of entries — until the 10-second `TimeoutIterator` fires. Issuing this request in a tight loop from multiple concurrent connections sustains the exhaustion. The root cause is at: [7](#0-6) 

where no `.take(self.request_limit)` guard exists, unlike the analogous path in `get_cells`: [8](#0-7)

### Citations

**File:** rpc/src/module/indexer.rs (L396-403)
```rust
    #[rpc(name = "get_cells")]
    fn get_cells(
        &self,
        search_key: IndexerSearchKey,
        order: IndexerOrder,
        limit: Uint32,
        after: Option<JsonBytes>,
    ) -> Result<IndexerPagination<IndexerCell>>;
```

**File:** rpc/src/module/indexer.rs (L879-883)
```rust
    #[rpc(name = "get_cells_capacity")]
    fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>>;
```

**File:** util/indexer/src/service.rs (L52-62)
```rust
impl<I: Iterator> Iterator for TimeoutIterator<I> {
    type Item = I::Item;

    fn next(&mut self) -> Option<Self::Item> {
        if self.start_time.elapsed() > self.timeout {
            self.timed_out = true;
            return None;
        }
        self.inner.next()
    }
}
```

**File:** util/indexer/src/service.rs (L93-100)
```rust
        Self {
            store,
            sync,
            block_filter: config.block_filter.clone(),
            cell_filter: config.cell_filter.clone(),
            request_limit: config.request_limit.unwrap_or(usize::MAX),
            timeout_limit: Duration::from_secs(config.timeout_limit.unwrap_or(10)),
        }
```

**File:** util/indexer/src/service.rs (L371-372)
```rust
            .take(limit)
            .collect::<Vec<_>>();
```

**File:** util/indexer/src/service.rs (L718-728)
```rust
        let mode = IteratorMode::From(from_key.as_ref(), direction);
        let snapshot = self.store.inner().snapshot();
        let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
        let pool = self
            .pool
            .as_ref()
            .map(|pool| pool.read().expect("acquire lock"));

        let capacity: u64 = iter
            .by_ref()
            .take_while(|(key, _value)| key.starts_with(&prefix))
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L26-27)
```rust
        let mut query_builder = SqlBuilder::select_from("output");
        query_builder.field("CAST(SUM(output.capacity) AS BIGINT) AS total_capacity");
```
