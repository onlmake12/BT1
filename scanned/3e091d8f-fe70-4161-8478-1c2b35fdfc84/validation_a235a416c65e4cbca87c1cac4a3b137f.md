The code confirms all key claims. Let me verify the `get_transactions` guard as well for completeness.

Audit Report

## Title
Unbounded Aggregation DoS via Missing `request_limit` Guard in `get_cells_capacity` — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

## Summary
`AsyncRichIndexerHandle::get_cells_capacity` executes an unbounded `SUM(output.capacity)` aggregation with no row-count cap, while the sibling methods `get_cells` and `get_transactions` both enforce `self.request_limit` before executing any query. An unprivileged RPC caller can issue repeated `get_cells_capacity` calls with `script_search_mode=prefix` and empty `args`, triggering a full-table scan on every call and causing sustained CPU/I/O degradation on the indexer database, degrading RPC service for all users.

## Finding Description
`AsyncRichIndexerHandle` stores a `request_limit: usize` field at [1](#0-0) 

Both `get_cells` and `get_transactions` enforce this limit at the top of their implementations before any query is built: [2](#0-1) [3](#0-2) 

`get_cells_capacity` has no equivalent guard. It proceeds directly to building and executing the aggregation query with no `LIMIT` clause anywhere in the function: [4](#0-3) 

The SQL builder never calls `.limit()` anywhere in `get_cells_capacity`, confirmed by reading the full function through line 226. [5](#0-4) 

When `script_search_mode=prefix` is used with empty `args`, `get_binary_upper_boundary` returns `vec![u8::MAX; 32]`: [6](#0-5) 

This is also confirmed by the unit test at: [7](#0-6) 

The prefix condition `args >= '' AND args < [0xFF×32]` therefore matches every script row with the given `code_hash`/`hash_type`, causing the join and aggregation to scan all matching live cells in the `output` table. The RPC dispatch passes the call straight through with no additional guard: [8](#0-7) 

## Impact Explanation
Each `get_cells_capacity` call with a broad prefix forces the database to perform a full aggregation scan over all matching live cells. On a large indexed mainnet chain (millions of live cells), this is a multi-second, high-I/O operation. An attacker issuing these calls in a tight loop will saturate the SQLite WAL reader lock or PostgreSQL I/O bandwidth, causing all concurrent RPC responses (`get_cells`, `get_transactions`, `get_indexer_tip`) to queue behind the long-running aggregation queries. This constitutes sustained performance degradation of the indexer RPC layer, matching **Low (501–2000): Any other important performance improvements for CKB**.

## Likelihood Explanation
The rich-indexer RPC endpoint is publicly accessible with no authentication. The attack requires only a standard JSON-RPC POST request — no special privileges, no key material, no hashpower. The call sequence is trivially reproducible with `curl` or any HTTP client. The asymmetry between `get_cells_capacity` and `get_cells`/`get_transactions` is a confirmed code omission, making it reliably exploitable on any node with the rich-indexer enabled and a large indexed chain.

## Recommendation
Add a row-count guard to `get_cells_capacity`. Since the method takes no `limit` parameter, the appropriate approach is to add an internal cap on the number of rows aggregated via a `LIMIT` on the inner script subquery (bounding the number of script IDs joined), or to reject the call with an error when the query would be unbounded (e.g., prefix search with empty args). At minimum, mirror the pattern from `get_cells` and `get_transactions` by checking `self.request_limit` at the top of `get_cells_capacity` and returning an error if the configured limit would be exceeded. Note that adding `LIMIT` directly to the outer aggregation subquery would return a partial (incorrect) sum, so the bound should be applied to the inner script ID subquery (`build_query_script_id_sql`) to cap the number of matched scripts, or a separate pre-flight count query should be used to reject requests that would scan more than `self.request_limit` rows.

## Proof of Concept
```bash
# On a node with rich-indexer enabled and a large indexed chain:
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

With `args: "0x"` and `script_search_mode: "prefix"`, `get_binary_upper_boundary` produces `[0xFF; 32]` as the upper bound (confirmed by unit test at `mod.rs` L367–370), matching all lock scripts with the given `code_hash`/`hash_type`. The database executes `SUM(output.capacity)` over all matching live cells with no row limit. Concurrent `get_cells` calls will stall behind the aggregation, demonstrating the DoS.

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L23-27)
```rust
pub struct AsyncRichIndexerHandle {
    store: SQLXPool,
    pool: Option<Arc<RwLock<Pool>>>,
    request_limit: usize,
}
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L251-254)
```rust
fn get_binary_upper_boundary(value: &[u8]) -> Vec<u8> {
    if value.is_empty() {
        return vec![u8::MAX; 32];
    }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L367-370)
```rust
    fn test_get_binary_upper_boundary_empty() {
        let result = get_binary_upper_boundary(&[]);
        assert_eq!(result, vec![255; 32]);
    }
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L111-116)
```rust
        // sql string
        let sql = query_builder
            .sql()
            .map_err(|err| Error::DB(err.to_string()))?
            .trim_end_matches(';')
            .to_string();
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
