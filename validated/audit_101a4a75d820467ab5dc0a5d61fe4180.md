### Title
Unbounded Aggregation DoS via `get_cells_capacity` Missing `request_limit` Guard — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

---

### Summary

`AsyncRichIndexerHandle::get_cells_capacity` performs an unbounded `SUM(output.capacity)` full-table aggregation with no row-count cap, while the sibling methods `get_cells` and `get_transactions` both enforce `self.request_limit`. An unprivileged RPC caller can issue repeated `get_cells_capacity` calls with `script_search_mode=prefix` and empty `args`, triggering a full-table scan on every call and causing sustained CPU/I/O degradation on the indexer database.

---

### Finding Description

`AsyncRichIndexerHandle` stores a `request_limit: usize` field: [1](#0-0) 

Both `get_cells` and `get_transactions` enforce this limit before executing any query: [2](#0-1) [3](#0-2) 

`get_cells_capacity` has no equivalent guard. It directly builds and executes an aggregation query with no `LIMIT` clause: [4](#0-3) 

The generated SQL is effectively:
```sql
SELECT CAST(SUM(output.capacity) AS BIGINT) AS total_capacity
FROM output
JOIN (SELECT script.id FROM script
      WHERE code_hash = $1 AND hash_type = $2
        AND args >= $3 AND args < $4) AS query_script
  ON output.lock_script_id = query_script.id
WHERE output.is_spent = 0
```

No `LIMIT` is ever appended to this query.

When `script_search_mode=prefix` is used with empty `args` (`0x`), `get_binary_upper_boundary` returns `vec![u8::MAX; 32]`: [5](#0-4) 

This means the prefix condition `args >= '' AND args < [0xFF×32]` matches **every** script row with the given `code_hash`/`hash_type`, causing the join and aggregation to scan all matching live cells in the `output` table — potentially millions of rows on a fully-indexed mainnet chain.

The RPC dispatch in `rich_indexer.rs` passes the call straight through with no additional guard: [6](#0-5) 

---

### Impact Explanation

Each `get_cells_capacity` call with a broad prefix forces the database to perform a full aggregation scan. On a large indexed chain (millions of live cells), this is a multi-second, high-I/O operation. A single attacker issuing these calls in a tight loop will saturate the SQLite WAL reader lock or Postgres I/O bandwidth, causing all concurrent RPC responses (`get_cells`, `get_transactions`, `get_indexer_tip`) to queue behind the long-running aggregation queries. This degrades the node's RPC service for all users. Impact is sustained performance degradation (DoS of the indexer RPC layer), matching the Low 501–2000 scope.

---

### Likelihood Explanation

The rich-indexer RPC endpoint is publicly accessible with no authentication. The attack requires only a standard JSON-RPC POST request — no special privileges, no key material, no hashpower. The call sequence is trivially reproducible with `curl` or any HTTP client. The asymmetry between `get_cells_capacity` and `get_cells`/`get_transactions` is a straightforward omission, not a design choice, making it reliably exploitable on any node with the rich-indexer enabled and a large indexed chain.

---

### Recommendation

Add the same `request_limit` guard to `get_cells_capacity`. Since the method takes no `limit` parameter, the appropriate fix is to add an internal row-count cap to the aggregation query (e.g., `LIMIT self.request_limit` on the subquery joining `output` to `query_script`), or to reject the call with an error if the operator has configured `request_limit = 0` (unlimited) and the query would be unbounded. At minimum, mirror the pattern from `get_cells`:

```rust
// At the top of get_cells_capacity, before building the query:
// (using a configurable max-rows-to-aggregate constant)
if self.request_limit == 0 {
    return Err(Error::invalid_params("get_cells_capacity requires a non-zero request_limit"));
}
```

And add `LIMIT self.request_limit` to the inner subquery so the aggregation is bounded.

---

### Proof of Concept

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
