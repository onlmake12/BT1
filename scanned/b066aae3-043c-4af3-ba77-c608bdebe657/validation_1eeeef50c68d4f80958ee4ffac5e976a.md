The code confirms the asymmetry. Here is the analysis:

---

### Title
Unbounded `SUM(output.capacity)` Aggregation in `get_cells_capacity` Allows RPC-Level DoS — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

### Summary
`get_cells_capacity` in the rich-indexer performs a full-table `SUM(output.capacity)` aggregation with no row-count bound, while the sibling methods `get_cells` and `get_transactions` both enforce `self.request_limit`. An unprivileged caller can issue repeated requests with `script_search_mode=prefix` and empty `args`, causing a full-table scan over every live cell in the `output` table, degrading all concurrent RPC responses.

### Finding Description

`AsyncRichIndexerHandle` carries a `request_limit` field: [1](#0-0) 

Both `get_cells` and `get_transactions` enforce it before executing any query: [2](#0-1) [3](#0-2) 

`get_cells_capacity` has no such guard. It directly builds and executes an unbounded aggregation query: [4](#0-3) 

The generated SQL is:
```sql
SELECT CAST(SUM(output.capacity) AS BIGINT) AS total_capacity
FROM output
JOIN (SELECT script.id FROM script
      WHERE code_hash = $1 AND hash_type = $2
        AND args >= $3 AND args < $4) AS query_script
  ON output.lock_script_id = query_script.id
WHERE output.is_spent = 0
```

When `args` is empty (`0x`), `get_binary_upper_boundary` returns `[0xFF; 32]`: [5](#0-4) 

This makes the script subquery match **every script row** with the given `code_hash`/`hash_type`, and the outer query aggregates every matching live cell — potentially millions of rows — with no `LIMIT` clause applied anywhere in `get_cells_capacity`. [6](#0-5) 

### Impact Explanation
Each call forces the database engine to perform a full join + aggregation scan over the entire live-cell set. On a large indexed chain (millions of outputs), this saturates I/O and CPU on the SQLite/PostgreSQL backend, blocking all concurrent indexer queries for the duration of the scan. Repeated calls in a tight loop compound the effect, causing sustained degradation of all RPC responses that depend on the rich-indexer.

### Likelihood Explanation
The rich-indexer RPC endpoint is publicly reachable by any unprivileged caller when the node operator enables it (the default configuration). No authentication, PoW, or rate-limiting is required. The call sequence is a single standard JSON-RPC POST with a valid `IndexerSearchKey`. The `request_limit` configuration exists and is wired into the handle, but is simply never consulted by `get_cells_capacity`.

### Recommendation
Add the same `request_limit` guard to `get_cells_capacity`, or — since it is an aggregation rather than a paginated result — impose a maximum-row-count hint at the SQL level (e.g., a `LIMIT` on the inner subquery or a configurable timeout/row-count cap on the aggregation query). At minimum, mirror the pattern already used in `get_cells`:

```rust
// At the top of get_cells_capacity, before building the query:
if /* some configurable cap is exceeded */ {
    return Err(Error::invalid_params("query would exceed maximum allowed rows"));
}
```

Alternatively, expose a `max_capacity_query_rows` config option and enforce it via a `LIMIT` on the script subquery.

### Proof of Concept
```bash
# On a node with rich-indexer enabled and a large indexed chain:
while true; do
  curl -s -X POST http://localhost:8114 \
    -H 'Content-Type: application/json' \
    -d '{
      "jsonrpc":"2.0","id":1,
      "method":"get_cells_capacity",
      "params":[{
        "script":{
          "code_hash":"0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
          "hash_type":"type",
          "args":"0x"
        },
        "script_type":"lock",
        "script_search_mode":"prefix"
      }]
    }'
done
```

Each iteration issues a `SUM(output.capacity)` scan over all live cells matching the given `code_hash`/`hash_type` (with empty-prefix matching all args). Wall-clock time grows linearly with the number of indexed outputs, while a concurrent `get_cells` call with `limit=request_limit` returns in bounded time.

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
