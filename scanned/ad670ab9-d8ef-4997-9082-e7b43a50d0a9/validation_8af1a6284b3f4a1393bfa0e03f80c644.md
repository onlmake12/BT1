Audit Report

## Title
Unbounded Full-Table Scan via `partial` Search Mode in Rich Indexer RPC Causes Denial of Service — (`File: util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

## Summary
The Rich Indexer's `get_cells_capacity` RPC endpoint accepts a caller-controlled `script_search_mode: "partial"` parameter that, on the PostgreSQL backend, generates a leading-wildcard `LIKE '%data%'` pattern that cannot use B-tree indexes and forces a full sequential scan. Unlike `get_cells` and `get_transactions`, `get_cells_capacity` enforces no row limit and issues an unbounded `SUM(capacity)` aggregate. Combined with `request_limit` defaulting to `usize::MAX` and `rpc_batch_limit` disabled by default, concurrent requests can exhaust the PostgreSQL connection pool and render the Rich Indexer RPC unresponsive.

## Finding Description
**Root cause 1 — Non-indexable LIKE pattern.**
`build_query_script_id_sql` in `util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs` emits `args LIKE $N` for the Postgres partial path. The bound value is produced by `escape_and_wrap_for_postgres_like` (lines 339–360), which inserts `0x25` (`%`) at both ends of the caller-supplied bytes, yielding `%<user_data>%`. A leading-wildcard LIKE pattern is non-sargable in PostgreSQL; no B-tree index on `script.args` can be used, forcing a sequential scan of the entire `script` table.

**Root cause 2 — No row limit in `get_cells_capacity`.**
`get_cells_capacity` (lines 26–27 of `get_cells_capacity.rs`) issues `SELECT CAST(SUM(output.capacity) AS BIGINT) AS total_capacity FROM output ...` with no `LIMIT` clause. The query must scan and aggregate every matching row before returning. By contrast, `get_cells` (lines 29–34 of `get_cells.rs`) explicitly checks `limit as usize > self.request_limit` and rejects oversized requests. `get_cells_capacity` has no equivalent guard.

**Root cause 3 — `request_limit` defaults to `usize::MAX`.**
`RichIndexerService::new` in `util/rich-indexer/src/service.rs` line 51: `request_limit: config.request_limit.unwrap_or(usize::MAX)`. The `request_limit` key is commented out in `resource/ckb.toml` (lines 286–291), so the default is effectively unlimited.

**Root cause 4 — `rpc_batch_limit` disabled by default.**
`rpc/src/server.rs` line 53–55 only sets `JSONRPC_BATCH_LIMIT` if `config.rpc_batch_limit` is `Some`; the config entry is commented out in `resource/ckb.toml` lines 205–208, leaving no batch-level throttle.

**Exploit flow:**
1. Attacker sends N concurrent `get_cells_capacity` requests with `script_search_mode: "partial"` and a 1-byte `args` value (e.g., `0x41`).
2. Each request opens a PostgreSQL transaction and executes a full sequential scan of `script` joined with `output`, aggregating all matching rows.
3. With no LIMIT, no `request_limit` guard, and no per-IP rate limiter, all N connections are held for the duration of the scan.
4. The PostgreSQL connection pool is exhausted; subsequent Rich Indexer RPC calls queue indefinitely or return errors.

## Impact Explanation
**Note (0–500 points) — Local RPC API crash/unavailability.**
The attack renders the Rich Indexer RPC surface unresponsive. The Rich Indexer is an opt-in feature (`--rich-indexer`) and the RPC binds to `127.0.0.1` by default, limiting the blast radius to nodes where the operator has both enabled the PostgreSQL-backed Rich Indexer and exposed the RPC endpoint. The impact is DoS of the Rich Indexer RPC, not a crash of the core CKB node, consensus engine, or P2P layer. The claim's assertion of "High" severity is not supported: no evidence is provided that the shared thread pool causes the core node RPC to fail, and the prerequisite operator configuration (PostgreSQL backend, exposed RPC) narrows the realistic impact to the Rich Indexer RPC surface. The correct in-scope category is **Note: Any local RPC API crash**.

## Likelihood Explanation
Triggerable by any unprivileged caller who can reach the RPC endpoint. Requires the operator to have enabled `--rich-indexer` with a PostgreSQL backend and exposed the RPC beyond localhost — a documented and recommended production configuration. The `partial` search mode is an advertised feature requiring no special privileges. The attack is trivially reproducible with standard `curl` commands and no authentication.

## Recommendation
1. Apply the same `request_limit` guard used in `get_cells` to `get_cells_capacity` — reject requests when the estimated result set would exceed the configured limit.
2. Change `config.request_limit.unwrap_or(usize::MAX)` in `util/rich-indexer/src/service.rs` line 51 to a bounded default (e.g., `400`, as already recommended in the commented-out `ckb.toml` line 291).
3. Enforce the already-documented `timeout_limit` (commented out at `resource/ckb.toml` line 293) as a hard default (e.g., 10 seconds) rather than opt-in.
4. Enable `rpc_batch_limit` by default (the comment at `resource/ckb.toml` lines 205–208 already recommends `2000`).
5. Consider setting a PostgreSQL `statement_timeout` at the connection level in `SQLXPool` so runaway scans are killed by the database regardless of application-level controls.

## Proof of Concept
```bash
# Requires: CKB node with --rich-indexer + PostgreSQL backend, RPC accessible
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
`args: "0x41"` causes `escape_and_wrap_for_postgres_like` to produce `%A%`, matching a large fraction of script rows and forcing a full table scan. With 300 concurrent requests and no rate limit or timeout, the PostgreSQL connection pool is exhausted and subsequent Rich Indexer RPC calls stall or return errors.