Audit Report

## Title
Unbounded Aggregate Query via `partial` Search Mode in `get_cells_capacity` Causes Rich Indexer RPC Denial of Service — (`File: util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

## Summary
The `get_cells_capacity` RPC handler in the CKB Rich Indexer accepts a caller-controlled `script_search_mode: "partial"` parameter that, on a PostgreSQL backend, generates a leading-wildcard `LIKE '%data%'` predicate against the `script.args` column — a non-sargable pattern that forces a full sequential scan. Unlike `get_cells` and `get_transactions`, `get_cells_capacity` performs no `request_limit` check and issues an unbounded `SUM(capacity)` aggregate with no `LIMIT` clause. Concurrent requests with a high-selectivity pattern can saturate the PostgreSQL connection pool and stall all Rich Indexer RPC consumers for the duration of each scan window.

## Finding Description

**Step 1 — Partial mode generates a non-indexable LIKE pattern.**

`build_query_script_id_sql` in `util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs` (lines 145–155) emits `args LIKE $N` for the Postgres path under `IndexerSearchMode::Partial`. The bound value is produced by `escape_and_wrap_for_postgres_like` (lines 339–360), which wraps caller-supplied bytes with `%` on both sides, yielding `%<user_data>%`. A leading-wildcard `LIKE` pattern is non-sargable in PostgreSQL — no B-tree index on `script.args` can be used, forcing a sequential scan of the entire `script` table.

**Step 2 — `get_cells_capacity` has no row limit and no `request_limit` guard.**

`get_cells.rs` (lines 29–34) explicitly checks `limit as usize > self.request_limit` and returns an error. `get_cells_capacity.rs` (lines 26–27) issues `CAST(SUM(output.capacity) AS BIGINT) AS total_capacity` with no `LIMIT` clause and no analogous `request_limit` check. Every matching row in `output` must be scanned and aggregated before the query returns.

**Step 3 — `request_limit` defaults to `usize::MAX`.**

`RichIndexerService::new` in `util/rich-indexer/src/service.rs` (line 51) sets `request_limit: config.request_limit.unwrap_or(usize::MAX)`. The `request_limit` key is commented out in `resource/ckb.toml` (lines 286–291), so the default is effectively unlimited.

**Step 4 — No per-method rate limiter; HTTP timeout provides partial mitigation.**

`rpc/src/server.rs` (lines 125–128) applies a `TimeoutLayer` of 30 seconds at the HTTP layer. This cancels the async future (and the in-flight sqlx query) after 30 seconds, which limits each individual request's damage window. However, it does not prevent an attacker from stacking hundreds of concurrent requests, each holding a PostgreSQL connection for up to 30 seconds, saturating the connection pool and starving legitimate callers for the full 30-second window per attack wave. `rpc_batch_limit` is also disabled by default (lines 205–208 of `ckb.toml`).

**Resulting query (PostgreSQL, partial mode):**
```sql
SELECT CAST(SUM(output.capacity) AS BIGINT) AS total_capacity
FROM output
JOIN (
  SELECT script.id FROM script
  WHERE code_hash = $1 AND hash_type = $2
    AND args LIKE $3   -- $3 = '%\x41%', non-sargable
) AS query_script ON output.lock_script_id = query_script.id
WHERE output.is_spent = 0;
```

## Impact Explanation

The attack causes the Rich Indexer RPC service to become unresponsive for the duration of each attack wave (up to 30 seconds per wave, repeatable continuously). All Rich Indexer RPC methods — `get_cells`, `get_transactions`, `get_indexer_tip`, and `get_cells_capacity` — share the same PostgreSQL connection pool and stall when it is exhausted. This constitutes a local RPC API crash/unresponsiveness.

**Impact: Note (0–500 points)** — local RPC API crash/unresponsiveness. The Rich Indexer is an opt-in feature; the RPC binds to `127.0.0.1` by default; and the 30-second HTTP timeout bounds each request's damage window. The impact does not reach the threshold for node crash, consensus deviation, or network congestion.

## Likelihood Explanation

The `partial` search mode is a documented, advertised feature. No authentication is required. The attack requires only standard JSON-RPC POST requests. Operators who expose the RPC port (common for dApp backends and block explorers) are reachable by external callers. The 30-second HTTP timeout means the attack must be sustained continuously to maintain the DoS, but this is trivially achievable with a simple loop. The missing `request_limit` guard in `get_cells_capacity` (compared to `get_cells` and `get_transactions`) is a concrete, reproducible asymmetry.

## Recommendation

1. Add a `request_limit` guard to `get_cells_capacity` analogous to the one in `get_cells` (lines 29–34 of `get_cells.rs`), rejecting requests that would exceed a configured row budget.
2. Change `config.request_limit.unwrap_or(usize::MAX)` in `service.rs` (line 51) to a bounded default (e.g., `400`, as already recommended in the `ckb.toml` comment at line 291).
3. Enforce the documented `timeout_limit` option (commented out at `ckb.toml` line 293) as a hard default (e.g., 10 seconds) at the database query level, or issue `SET statement_timeout` at connection time in `SQLXPool`.
4. Enable `rpc_batch_limit` by default (the comment at `ckb.toml` lines 205–208 already recommends `2000`).

## Proof of Concept

```bash
# CKB node with --rich-indexer and PostgreSQL backend
# Sends 300 concurrent get_cells_capacity requests with partial mode
for i in $(seq 1 300); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{
      "id": '"$i"',
      "jsonrpc": "2.0",
      "method": "get_cells_capacity",
      "params": [{
        "script": {
          "code_hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
          "hash_type": "data",
          "args": "0x41"
        },
        "script_type": "lock",
        "script_search_mode": "partial"
      }]
    }' &
done
wait
```

`args: "0x41"` causes `escape_and_wrap_for_postgres_like` to produce `%A%`, matching a large fraction of script rows and forcing a full table scan. With 300 concurrent requests and no `request_limit` guard in `get_cells_capacity`, the PostgreSQL connection pool is exhausted and subsequent Rich Indexer RPC calls stall or return errors until the 30-second HTTP timeout clears each connection.