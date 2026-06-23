The code evidence is now clear. Let me summarize the findings.

**Key facts confirmed:**

1. `get_cells` in `AsyncRichIndexerHandle` enforces `request_limit` at lines 29–34: [1](#0-0) 

2. `get_transactions` in `AsyncRichIndexerHandle` also enforces `request_limit` at lines 23–32: [2](#0-1) 

3. `get_cells_capacity` in `AsyncRichIndexerHandle` has **no** `request_limit` check and **no** `LIMIT` clause — it issues an unbounded `SUM(output.capacity)` aggregation: [3](#0-2) 

4. `AsyncRichIndexerHandle` carries the `request_limit` field but `get_cells_capacity` never reads it: [4](#0-3) 

5. With `partial` mode, `build_query_script_id_sql` generates `args LIKE $N` (Postgres) or `instr(args, $N) > 0` (SQLite): [5](#0-4) 

6. For Postgres with empty args, `escape_and_wrap_for_postgres_like` produces `%%` (two `%` wildcards), which matches every row in the `script` table: [6](#0-5) 

7. The old RocksDB-based `IndexerHandle::get_cells_capacity` is protected by `TimeoutIterator`, but the rich-indexer has no equivalent timeout: [7](#0-6) 

8. `RichIndexerConfig` has no `request_limit` field at all, and tests initialize with `usize::MAX`: [8](#0-7) 

---

### Title
Missing `request_limit` and timeout in `AsyncRichIndexerHandle::get_cells_capacity` enables unbounded full-table aggregation scan — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

### Summary
`AsyncRichIndexerHandle::get_cells_capacity` issues an unbounded `SUM(output.capacity)` SQL aggregation with no row limit and no query timeout. Every sibling method (`get_cells`, `get_transactions`) checks `self.request_limit` before executing. `get_cells_capacity` never consults `request_limit` and never appends a `LIMIT` clause. An unprivileged caller who can reach the RPC port can repeatedly invoke this endpoint with `script_search_mode=partial` and empty args (producing `LIKE '%%'` on Postgres), forcing a full-table join-and-aggregate over the entire `output` table on every call.

### Finding Description
In `get_cells.rs` lines 29–34 and `get_transactions.rs` lines 27–32, the handler immediately returns an error if the caller-supplied `limit` exceeds `self.request_limit`. In `get_cells_capacity.rs`, no such guard exists. The function builds a `SELECT CAST(SUM(output.capacity) AS BIGINT) AS total_capacity FROM output JOIN (SELECT id FROM script WHERE code_hash=$1 AND hash_type=$2 AND args LIKE $3) AS query_script ON ...` query and executes it with `fetch_optional`, which blocks until the DB engine finishes scanning every matching row. No `LIMIT`, no `STATEMENT_TIMEOUT`, and no `TimeoutIterator` equivalent is applied.

When `script_search_mode=partial` and `args` is empty bytes, `escape_and_wrap_for_postgres_like` produces the two-byte sequence `[0x25, 0x25]` (`%%`), which is a valid PostgreSQL `LIKE` pattern matching any string. The script subquery therefore returns every row in the `script` table, and the outer join aggregates every live `output` row.

### Impact Explanation
Each such call holds one of the 10 SQLXPool connections for the full duration of the aggregation scan. Concurrent calls exhaust the pool, blocking all other indexer RPC handlers and the indexer sync writer (which also uses the same pool). On a mainnet-sized database (tens of millions of output rows), each call can run for seconds to minutes, making the indexer service effectively unavailable for the duration of the attack.

### Likelihood Explanation
The RPC endpoint is reachable by any process that can connect to the node's RPC port. While the default binding is `127.0.0.1`, many operators expose the RPC publicly or via reverse proxy. The attack requires only a single valid JSON-RPC call with a known-good `code_hash`/`hash_type` and empty `args`. No authentication, no PoW, no key material is needed.

### Recommendation
Add the same guard that `get_cells` and `get_transactions` use — but adapted for the aggregation context. Since `get_cells_capacity` has no caller-supplied `limit`, the appropriate fix is to:
1. Add a `STATEMENT_TIMEOUT` (Postgres) or `busy_timeout`/pragma (SQLite) on the connection before executing the aggregation.
2. Optionally, reject `partial` mode with empty args explicitly, or add a configurable `max_scan_rows` hint via `LIMIT` on the inner script subquery.
3. Add `request_limit` to `RichIndexerConfig` with a sensible default (e.g., 1000) and enforce it in `get_cells_capacity` by limiting the number of matching script IDs in the subquery.

### Proof of Concept
```
# Against a Postgres-backed rich-indexer node with any indexed data:
curl -s -X POST http://<rpc_host>:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "id": 1, "jsonrpc": "2.0", "method": "get_cells_capacity",
    "params": [{
      "script": {
        "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
        "hash_type": "type",
        "args": "0x"
      },
      "script_type": "lock",
      "script_search_mode": "partial"
    }]
  }'
# Repeat concurrently 10+ times to exhaust the connection pool.
# Each call executes: SELECT SUM(output.capacity) FROM output JOIN
#   (SELECT id FROM script WHERE code_hash=? AND hash_type=? AND args LIKE '%%') ...
# with no LIMIT and no timeout, scanning the full output table.
```

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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L22-37)
```rust
#[derive(Clone)]
pub struct AsyncRichIndexerHandle {
    store: SQLXPool,
    pool: Option<Arc<RwLock<Pool>>>,
    request_limit: usize,
}

impl AsyncRichIndexerHandle {
    /// Construct new AsyncRichIndexerHandle instance
    pub fn new(store: SQLXPool, pool: Option<Arc<RwLock<Pool>>>, request_limit: usize) -> Self {
        Self {
            store,
            pool,
            request_limit,
        }
    }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L145-155)
```rust
        Some(IndexerSearchMode::Partial) => {
            match db_driver {
                DBDriver::Postgres => {
                    query_builder.and_where(format!("args LIKE ${}", param_index));
                }
                DBDriver::Sqlite => {
                    query_builder.and_where(format!("instr(args, ${}) > 0", param_index));
                }
            }
            *param_index += 1;
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L339-360)
```rust
fn escape_and_wrap_for_postgres_like(data: &JsonBytes) -> Vec<u8> {
    // 0x5c is the default escape character '\'
    // 0x25 is the '%' wildcard
    // 0x5f is the '_' wildcard

    let mut new_data: Vec<u8> = data
        .as_bytes()
        .iter()
        .flat_map(|&b| {
            if b == 0x25 || b == 0x5c || b == 0x5f {
                vec![0x5c, b]
            } else {
                vec![b]
            }
        })
        .collect();

    new_data.insert(0, 0x25); // Start with %
    new_data.push(0x25); // End with %

    new_data
}
```

**File:** util/indexer/src/service.rs (L718-720)
```rust
        let mode = IteratorMode::From(from_key.as_ref(), direction);
        let snapshot = self.store.inner().snapshot();
        let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
```

**File:** util/rich-indexer/src/tests/query.rs (L478-478)
```rust
    let indexer = AsyncRichIndexerHandle::new(pool.clone(), None, usize::MAX);
```
