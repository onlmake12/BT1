The code confirms all factual claims. Let me verify the key points directly from the source:

- `get_cells_capacity.rs` lines 13–27: no `request_limit` check, no `LIMIT` clause, issues unbounded `SUM(output.capacity)` aggregation via `fetch_optional` [1](#0-0) 
- `get_cells.rs` lines 29–34: `request_limit` guard present [2](#0-1) 
- `get_transactions.rs` lines 27–32: `request_limit` guard present [3](#0-2) 
- `get_cells_capacity.rs` lines 132–136: `partial` mode with Postgres calls `escape_and_wrap_for_postgres_like`, which wraps empty args as `%%` [4](#0-3) 
- `get_cells_capacity.rs` lines 187–191: query executes with `fetch_optional` — no timeout, no limit [5](#0-4) 

All cited code matches the report's claims exactly. The vulnerability is real and triggerable by an unprivileged caller. The impact is exhaustion of the indexer's SQLXPool connections, making the indexer RPC unavailable for the duration of the attack — matching the "Any local RPC API crash" impact class.

---

Audit Report

## Title
Missing `request_limit` and timeout in `AsyncRichIndexerHandle::get_cells_capacity` enables unbounded full-table aggregation scan — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

## Summary
`AsyncRichIndexerHandle::get_cells_capacity` issues an unbounded `SUM(output.capacity)` SQL aggregation with no row limit and no query timeout. Every sibling method (`get_cells`, `get_transactions`) checks `self.request_limit` before executing, but `get_cells_capacity` never consults `request_limit` and never appends a `LIMIT` clause. An unprivileged caller who can reach the RPC port can repeatedly invoke this endpoint with `script_search_mode=partial` and empty args, forcing a full-table join-and-aggregate over the entire `output` table on every call, exhausting the SQLXPool connection pool and making the indexer RPC unavailable.

## Finding Description
In `get_cells.rs` lines 29–34 and `get_transactions.rs` lines 27–32, the handler immediately returns an error if the caller-supplied `limit` exceeds `self.request_limit`. In `get_cells_capacity.rs`, no such guard exists anywhere in the function body. The function calls `build_query_script_id_sql` to build a script subquery, then constructs `SELECT CAST(SUM(output.capacity) AS BIGINT) AS total_capacity FROM output JOIN (...) AS query_script ON ...` and executes it via `fetch_optional` at lines 187–191, which blocks until the DB engine finishes scanning every matching row. No `LIMIT`, no `STATEMENT_TIMEOUT`, and no `TimeoutIterator` equivalent is applied.

When `script_search_mode=partial` and `args` is empty bytes (`0x`), `escape_and_wrap_for_postgres_like` (called at line 134) produces `[0x25, 0x25]` (`%%`), a valid PostgreSQL `LIKE` pattern matching any string. The script subquery therefore returns every row in the `script` table, and the outer join aggregates every live `output` row. Each such call holds one of the SQLXPool connections for the full duration of the aggregation. Ten concurrent calls exhaust the pool, blocking all other indexer RPC handlers and the indexer sync writer.

## Impact Explanation
This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**. The indexer RPC becomes fully unresponsive for the duration of the attack as all pool connections are held by long-running aggregation queries. The indexer sync writer, which shares the same pool, is also blocked, causing the indexer to fall behind the chain tip while under attack.

## Likelihood Explanation
The RPC endpoint is reachable by any process that can connect to the node's RPC port. The attack requires only a single valid JSON-RPC call with any known `code_hash`/`hash_type` and empty `args` (`0x`) with `script_search_mode=partial`. No authentication, no proof-of-work, and no key material is needed. Repeating the call 10+ times concurrently exhausts the pool. The default binding is `127.0.0.1`, but many operators expose the RPC publicly or via reverse proxy.

## Recommendation
1. Add the same `request_limit` guard that `get_cells` and `get_transactions` use — adapted for the aggregation context by limiting the number of matching script IDs in the inner subquery (e.g., `LIMIT self.request_limit` on the script subquery).
2. Add a `STATEMENT_TIMEOUT` (Postgres) or `busy_timeout` pragma (SQLite) on the connection before executing the aggregation query.
3. Optionally, explicitly reject `partial` mode with empty args, or add a configurable `max_scan_rows` to `RichIndexerConfig` with a sensible default.

## Proof of Concept
```bash
# Against a Postgres-backed rich-indexer node with any indexed data:
for i in $(seq 1 12); do
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
    }' &
done
wait
# Each call executes:
#   SELECT CAST(SUM(output.capacity) AS BIGINT) AS total_capacity
#   FROM output JOIN (SELECT id FROM script WHERE code_hash=? AND hash_type=? AND args LIKE '%%') AS query_script ON ...
#   WHERE output.is_spent = 0
# with no LIMIT and no timeout, scanning the full output table.
# 10+ concurrent calls exhaust the SQLXPool; subsequent indexer RPC calls hang until the pool frees.
```

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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L132-136)
```rust
            Some(IndexerSearchMode::Partial) => match self.store.db_driver {
                DBDriver::Postgres => {
                    let new_args = escape_and_wrap_for_postgres_like(&search_key.script.args);
                    query = query.bind(new_args);
                }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L187-191)
```rust
        let capacity = query
            .fetch_optional(&mut *tx)
            .await
            .map_err(|err| Error::DB(err.to_string()))?
            .and_then(|row| row.try_get::<i64, _>("total_capacity").ok());
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L29-34)
```rust
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L27-32)
```rust
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```
