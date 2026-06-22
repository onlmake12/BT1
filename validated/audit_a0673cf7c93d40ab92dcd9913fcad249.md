Now I have all the evidence needed. Let me analyze the key facts from the code.

The code evidence is conclusive. Here is the analysis:

---

### Title
Unbounded Aggregate Query in `get_cells_capacity` Enables RPC-Triggered DoS — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

### Summary

`AsyncRichIndexerHandle::get_cells_capacity` performs an unbounded `SUM(output.capacity)` aggregate over all matching rows with no row limit, while the sibling endpoints `get_cells` and `get_transactions` both enforce `self.request_limit` and emit a SQL `LIMIT` clause. An unprivileged RPC caller can trigger a full-table-scan aggregate by supplying `script_search_mode=Partial` with empty `args`, causing SQLite to evaluate `instr(args, '') > 0` (always true) and scan every output row matching the given `code_hash`/`hash_type`.

### Finding Description

**Missing `request_limit` guard in `get_cells_capacity`:**

`get_cells` enforces the limit at lines 26–34 and appends `LIMIT` to the SQL at line 156: [1](#0-0) [2](#0-1) 

`get_transactions` does the same at lines 24–32 and line 179: [3](#0-2) 

`get_cells_capacity` has **no such check and no `LIMIT` clause**. The entire function body builds and executes an unbounded aggregate: [4](#0-3) 

**`instr(args, '') > 0` is always true on SQLite:**

`build_query_script_id_sql` (used by `get_cells_capacity`) generates this condition for SQLite + Partial mode: [5](#0-4) 

When the caller passes empty `args`, the bound parameter is `b""`. SQLite's `instr(X, '')` returns `1` for every row (the empty string is found at position 1 in any string), so the script subquery returns **all** `script` rows matching the given `code_hash`/`hash_type`. The outer query then aggregates `SUM(output.capacity)` over every live output joined to those scripts — with no row limit.

**The `request_limit` field exists on the handle but is never consulted:** [6](#0-5) 

### Impact Explanation

An attacker with access to the RPC endpoint (default: localhost, but commonly exposed by node operators) can repeatedly call `get_cells_capacity` targeting a high-cardinality lock script (e.g., the secp256k1 default lock, which covers the majority of mainnet outputs). Each call forces a full sequential scan of all matching output rows and a CPU-bound aggregation with no early termination. Concurrent calls compound the effect. This can starve the SQLite connection pool and block all other indexer RPC responses, effectively denying service to legitimate users.

### Likelihood Explanation

The RPC requires no authentication. The attack payload is a single valid JSON-RPC call. The `code_hash` of the secp256k1 default lock is public knowledge. The condition (`Partial` + empty `args`) is a normal, accepted input that passes all input validation. The asymmetry between `get_cells_capacity` and its siblings is a straightforward oversight, not a deliberate design choice.

### Recommendation

Add the same `request_limit` guard that `get_cells` and `get_transactions` use. Since `get_cells_capacity` is an aggregate (no caller-supplied `limit` parameter), the guard should be applied at the SQL level — e.g., add a `LIMIT` to the script subquery or reject requests when the estimated matching script count exceeds `self.request_limit`. Additionally, validate that `args` is non-empty when `script_search_mode=Partial` is specified, or treat empty `args` as an error for that mode.

### Proof of Concept

```
# Index a chain with many outputs under the secp256k1 default lock.
# Then call:
curl -X POST http://localhost:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "method": "get_cells_capacity",
    "params": [{
      "script": {
        "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
        "hash_type": "type",
        "args": "0x"
      },
      "script_type": "lock",
      "script_search_mode": "partial"
    }],
    "id": 1
  }'
# SQLite executes: instr(args, '') > 0  -- always true
# SUM scans every live output with that code_hash/hash_type, no LIMIT.
# Repeat in a tight loop; compare wall-clock time to get_cells with the same key
# (which returns after request_limit rows). get_cells_capacity will be orders of
# magnitude slower and will not terminate early regardless of table size.
```

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L26-34)
```rust
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L156-156)
```rust
        query_builder.limit(limit);
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L24-32)
```rust
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L13-30)
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
        query_builder.join(format!("{} AS query_script", script_sub_query_sql));
        match search_key.script_type {
            IndexerScriptType::Lock => {
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L23-37)
```rust
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L145-154)
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
```
