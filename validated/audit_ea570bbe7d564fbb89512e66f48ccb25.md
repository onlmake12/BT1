Audit Report

## Title
Unbounded `get_cells_capacity` Aggregation Enables DoS on Indexer-Enabled Nodes — (File: `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`, `util/indexer/src/service.rs`)

## Summary
The `get_cells_capacity` RPC endpoint accepts a `search_key` with no `limit` parameter and no server-side row cap, unlike the sibling `get_cells` and `get_transactions` endpoints which both enforce a caller-supplied limit validated against `request_limit`. In the rich indexer, this results in a fully unbounded SQL `SUM` aggregation over the entire `output` table. In the basic indexer, a `TimeoutIterator` provides a partial time-based mitigation (default 10 seconds), but no row-count cap exists. Any unprivileged caller can force the rich indexer to scan and aggregate every matching live cell per request, with no bound on work performed.

## Finding Description
`get_cells` and `get_transactions` in the basic indexer both validate the caller-supplied limit against `self.request_limit` before iterating:

```rust
// util/indexer/src/service.rs:212-221
let limit = limit.value() as usize;
if limit == 0 {
    return Err(Error::invalid_params("limit should be greater than 0"));
}
if limit > self.request_limit {
    return Err(Error::invalid_params(...));
}
```

`get_cells_capacity` in the same file has no such check. It proceeds directly to iterate all matching cells:

```rust
// util/indexer/src/service.rs:686-720
pub fn get_cells_capacity(&self, search_key: IndexerSearchKey) -> Result<...> {
    // ... no limit or request_limit check ...
    let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
```

The basic indexer's `TimeoutIterator` provides a time-based bound (default 10 seconds, configurable), which partially mitigates the risk but does not cap the number of rows scanned within that window.

In the rich indexer, `get_transactions` enforces `request_limit`:

```rust
// util/rich-indexer/.../get_transactions.rs:23-32
let limit = limit.value();
if limit == 0 { return Err(...); }
if limit as usize > self.request_limit { return Err(...); }
```

But `get_cells_capacity` in the rich indexer has no equivalent check and issues an unbounded SQL aggregation:

```rust
// util/rich-indexer/.../get_cells_capacity.rs:26-27
let mut query_builder = SqlBuilder::select_from("output");
query_builder.field("CAST(SUM(output.capacity) AS BIGINT) AS total_capacity");
```

No `LIMIT` clause, no timeout, no row cap is applied to this query. The query runs to completion regardless of how many rows match. The RPC trait definition confirms no `limit` parameter exists at the API surface:

```rust
// rpc/src/module/indexer.rs:879-883
#[rpc(name = "get_cells_capacity")]
fn get_cells_capacity(&self, search_key: IndexerSearchKey) -> Result<Option<IndexerCellsCapacity>>;
```

A caller using `script_search_mode: "prefix"` with an empty `args` (`0x`) matches every cell indexed under any lock script sharing the given `code_hash` prefix, maximizing scan width. The rich indexer holds a database transaction open for the full duration of the aggregation, blocking concurrent writes.

## Impact Explanation
This matches **High: Vulnerabilities which could easily crash a CKB node**. Operators who enable the `RichIndexer` module (common for public nodes serving wallets and dapps) expose an endpoint where a single HTTP POST with a broad prefix search key forces a full-table `SUM` aggregation with no server-side bound. On a mature mainnet node with tens of millions of live cells, this saturates database I/O and CPU. Concurrent or rapid repeated calls can render the RPC server unresponsive, effectively crashing the node's RPC layer. The basic indexer is partially mitigated by `TimeoutIterator` (default 10 seconds), but the rich indexer has no mitigation whatsoever.

## Likelihood Explanation
Any unprivileged RPC caller can trigger this with a single HTTP POST. No authentication, no fee, no proof-of-work is required. The `Indexer` and `RichIndexer` modules are not enabled by default (`resource/ckb.toml` line 190), but operators who enable them for wallet/dapp support cannot selectively disable `get_cells_capacity` without disabling the entire module (and thus also `get_cells` and `get_transactions`). The attack is trivially repeatable and requires no special knowledge beyond the RPC schema.

## Recommendation
1. Add a server-side `request_limit` check to `get_cells_capacity` in the rich indexer (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`), analogous to the check already present in `get_transactions`.
2. Add a `LIMIT` clause to the SQL `SUM` subquery or enforce a maximum scan-row cap to bound the work per call.
3. For the basic indexer (`util/indexer/src/service.rs`), add an explicit row-count cap in addition to the existing `TimeoutIterator`, consistent with how `get_cells` uses `.take(limit)`.
4. Consider requiring a mandatory `block_range` filter or a minimum script specificity to prevent maximally broad prefix scans.

## Proof of Concept
Send the following request repeatedly to a node with `RichIndexer` enabled:

```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "get_cells_capacity",
  "params": [
    {
      "script": {
        "code_hash": "0x00",
        "hash_type": "type",
        "args": "0x"
      },
      "script_type": "lock",
      "script_search_mode": "prefix"
    }
  ]
}
```

The empty `args` (`0x`) with prefix mode matches every cell indexed under any lock script with the given `code_hash` prefix. Each call forces a full-table `SUM` aggregation in the rich indexer with no server-side bound. Concurrent calls compound the effect. The basic indexer will return an error after `timeout_limit` seconds (default 10s), but the rich indexer runs to completion on every call. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L13-27)
```rust
    pub async fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>, Error> {
        // sub query for script
        let mut param_index = 1;
        let script_sub_query_sql = build_query_script_id_sql(
            self.store.db_driver,
            &search_key.script_search_mode,
            &mut param_index,
        )?;

        // query output
        let mut query_builder = SqlBuilder::select_from("output");
        query_builder.field("CAST(SUM(output.capacity) AS BIGINT) AS total_capacity");
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

**File:** util/indexer/src/service.rs (L212-221)
```rust
        let limit = limit.value() as usize;
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/indexer/src/service.rs (L686-720)
```rust
    pub fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>, Error> {
        if search_key
            .script_search_mode
            .as_ref()
            .map(|mode| *mode == IndexerSearchMode::Partial)
            .unwrap_or(false)
        {
            return Err(Error::invalid_params(
                "the CKB indexer doesn't support search_key.script_search_mode partial search mode, \
                please use the CKB rich-indexer for such search",
            ));
        }

        let (prefix, from_key, direction, skip) = build_query_options(
            &search_key,
            KeyPrefix::CellLockScript,
            KeyPrefix::CellTypeScript,
            IndexerOrder::Asc,
            None,
        )?;
        let filter_script_type = match search_key.script_type {
            IndexerScriptType::Lock => IndexerScriptType::Type,
            IndexerScriptType::Type => IndexerScriptType::Lock,
        };
        let script_search_exact = matches!(
            search_key.script_search_mode,
            Some(IndexerSearchMode::Exact)
        );
        let filter_options: FilterOptions = search_key.try_into()?;
        let mode = IteratorMode::From(from_key.as_ref(), direction);
        let snapshot = self.store.inner().snapshot();
        let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
```

**File:** rpc/src/module/indexer.rs (L879-883)
```rust
    #[rpc(name = "get_cells_capacity")]
    fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>>;
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
