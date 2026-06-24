The code confirms all factual claims. Let me verify the `get_transactions` guard as well for completeness.

Audit Report

## Title
Unbounded Aggregation DoS via Missing `request_limit` Guard in `get_cells_capacity` — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

## Summary
`AsyncRichIndexerHandle::get_cells_capacity` executes an unbounded `SUM(output.capacity)` aggregation with no row-count cap, while the sibling methods `get_cells` and `get_transactions` both enforce `self.request_limit` before executing any query. An unprivileged RPC caller can repeatedly invoke `get_cells_capacity` with `script_search_mode=prefix` and empty `args`, forcing a full-table scan on every call and causing sustained CPU/I/O degradation on the indexer database.

## Finding Description
`AsyncRichIndexerHandle` stores a `request_limit: usize` field at [1](#0-0)  used to cap query scope.

`get_cells` enforces this limit at the top of the function before any query is built: [2](#0-1) 

`get_transactions` applies the identical guard: [3](#0-2) 

`get_cells_capacity` has no equivalent guard. It immediately builds and executes an aggregation query with no `LIMIT` clause: [4](#0-3) 

When `script_search_mode=prefix` is used with empty `args`, `get_binary_upper_boundary` returns `vec![u8::MAX; 32]`: [5](#0-4) 

This makes the prefix condition `args >= '' AND args < [0xFF×32]` match every script row with the given `code_hash`/`hash_type`, causing the join and aggregation to scan all matching live cells in the `output` table with no bound. The RPC dispatch passes the call straight through with no additional guard: [6](#0-5) 

## Impact Explanation
Each `get_cells_capacity` call with a broad prefix forces the database to perform a full aggregation scan over potentially millions of rows on a fully-indexed mainnet chain. A single attacker issuing these calls in a tight loop will saturate SQLite WAL reader locks or PostgreSQL I/O bandwidth, causing all concurrent RPC responses (`get_cells`, `get_transactions`, `get_indexer_tip`) to queue behind the long-running aggregation queries. This constitutes sustained performance degradation of the indexer RPC layer, matching **Low (501–2000 points): Any other important performance improvements for CKB**.

## Likelihood Explanation
The rich-indexer RPC endpoint is publicly accessible with no authentication. The attack requires only a standard JSON-RPC POST request — no special privileges, no key material, no hashpower. The call is trivially reproducible with `curl` or any HTTP client. The asymmetry between `get_cells_capacity` and `get_cells`/`get_transactions` is a straightforward omission, making it reliably exploitable on any node with the rich-indexer enabled and a large indexed chain.

## Recommendation
Add a row-count cap to `get_cells_capacity`. Since the method takes no caller-supplied `limit` parameter, the appropriate fix is to apply `LIMIT self.request_limit` to the inner subquery that joins `output` to `query_script`, bounding the number of rows the aggregation scans. Alternatively, if `request_limit == 0` (operator-configured unlimited), reject the call with an error mirroring the pattern in `get_cells` and `get_transactions`. This preserves the existing operator-configurable guard semantics across all three methods.

## Proof of Concept
On a node with rich-indexer enabled and a large indexed chain:

```bash
while true; do
  curl -s -X POST http://localhost:8116 \
    -H 'Content-Type: application/json' \
    -d '{
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
    }'
done
```

With `args: "0x"` and `script_search_mode: "prefix"`, `get_binary_upper_boundary` produces `[0xFF; 32]` as the upper bound, matching all lock scripts with the given `code_hash`/`hash_type`. The database executes `SUM(output.capacity)` over all matching live cells with no row limit. Concurrent `get_cells` calls will stall behind the aggregation, demonstrating the DoS.

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L23-27)
```rust
pub struct AsyncRichIndexerHandle {
    store: SQLXPool,
    pool: Option<Arc<RwLock<Pool>>>,
    request_limit: usize,
}
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L251-253)
```rust
fn get_binary_upper_boundary(value: &[u8]) -> Vec<u8> {
    if value.is_empty() {
        return vec![u8::MAX; 32];
```

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

**File:** rpc/src/module/rich_indexer.rs (L206-214)
```rust
    async fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>> {
        self.handle
            .get_cells_capacity(search_key)
            .await
            .map_err(|e| RPCError::custom(RPCError::Indexer, e))
    }
```
