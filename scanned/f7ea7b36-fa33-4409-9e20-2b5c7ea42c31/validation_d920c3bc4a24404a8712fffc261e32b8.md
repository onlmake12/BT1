All cited code references are confirmed in the repository. Every claim maps to actual code:

- `service.rs:51` — `request_limit: config.request_limit.unwrap_or(usize::MAX)` [1](#0-0) 
- `get_cells.rs:29-34` — guard fires only when `limit as usize > usize::MAX`, which is never true for a `u32` input [2](#0-1) 
- `get_cells.rs:156` — raw `limit` forwarded to `query_builder.limit(limit)` [3](#0-2) 
- `mod.rs:110` — `instr(args, $1) > 0` for SQLite Partial mode [4](#0-3) 
- `store.rs:143-148` — `fetch_all` holds pool connection for entire materialization [5](#0-4) 
- `store.rs:46-50` — pool capped at 10 connections, 60s acquire timeout [6](#0-5) 
- `store.rs:29-31` — `SQLXPool` wraps `Arc<OnceLock<AnyPool>>`, shared across clones [7](#0-6) 
- `indexer/mod.rs:156-161` — `append` acquires from the same shared pool via `transaction()` [8](#0-7) 

---

Audit Report

## Title
Connection Pool Exhaustion via Unbounded `get_cells` with `Partial` Script Search Mode — (`util/rich-indexer/src/`)

## Summary
When `request_limit` is absent from config, it defaults to `usize::MAX`, making the per-request `limit` guard a no-op for any `u32` input. An unprivileged caller can submit 10 concurrent `get_cells` requests with `limit=0xffffffff` and `script_search_mode=Partial`, each triggering a full-table `instr(args, $1) > 0` scan on SQLite while holding a pool connection via `fetch_all` for the entire duration. With the pool capped at 10 connections and a 60-second acquire timeout, all connections are exhausted, causing every subsequent indexer RPC call and every `append`/`rollback` sync operation to block for 60 seconds before failing.

## Finding Description
**Root cause — ineffective limit guard:**
`RichIndexerService::new` sets `request_limit: config.request_limit.unwrap_or(usize::MAX)`. The guard `if limit as usize > self.request_limit` compares a `u32`-derived `usize` against `usize::MAX`; on any 64-bit platform `u32::MAX` (4 294 967 295) is strictly less than `usize::MAX` (2⁶⁴−1), so the check never fires. The raw value is forwarded to `query_builder.limit(limit)`.

**Full-table scan via `Partial` mode:**
For SQLite, `Partial` mode emits `instr(args, $1) > 0`. The `instr()` function cannot use any B-tree index on the `args` column; SQLite must perform a sequential scan of the entire `script` table.

**Connection held for full scan duration:**
`fetch_all` materialises all matching rows before returning, keeping the connection checked out from the pool for the entire scan. With `limit=u32::MAX` and a full-table scan, this duration is unbounded.

**Shared pool exhaustion blocks sync:**
`SQLXPool` wraps `Arc<OnceLock<AnyPool>>` and is cloned into both the RPC handle and the indexer sync path. `AsyncRichIndexer::append` and `rollback` both call `self.store.transaction()`, which acquires from the same 10-connection pool. Ten concurrent attacker requests exhaust all connections; every subsequent RPC call and every sync operation blocks for 60 seconds before receiving a pool-timeout error.

## Impact Explanation
The impact is a repeatable, externally-triggered denial of service against the rich-indexer RPC surface: all indexer RPC methods (`get_cells`, `get_transactions`, `get_cells_capacity`, `get_indexer_tip`) become unresponsive for 60-second windows, and indexer sync stalls for the same duration. This matches **Note (0–500 points) — Any local RPC API crash**, as the indexer RPC is rendered non-functional for the duration of each attack wave. The core CKB consensus and p2p layers are unaffected; the impact is confined to the optional rich-indexer component and its RPC endpoints.

## Likelihood Explanation
The default configuration (no `request_limit` set) is the common deployment case. The RPC endpoint is routinely exposed on indexer nodes. The attack requires only 10 standard JSON-RPC calls with no authentication, no proof-of-work, and no privileged access. It is fully repeatable: as soon as one batch of connections times out or completes, the attacker fires another 10 requests.

## Recommendation
1. Replace `usize::MAX` with a safe default for `request_limit` (e.g., `100` or `1000`).
2. Add a hard upper bound on `limit` independent of `request_limit` (e.g., cap at `u16::MAX` or a fixed constant) so a misconfigured or absent `request_limit` cannot be exploited.
3. Apply a query-level timeout at the SQLx layer so long-running scans release their connections before the pool acquire timeout fires.
4. Consider a separate, smaller connection pool for RPC read queries versus indexer-sync write transactions to prevent cross-contamination.

## Proof of Concept
```bash
# CKB with rich-indexer, SQLite backend, no request_limit in config.
for i in $(seq 1 10); do
  curl -s -X POST http://localhost:8116 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"get_cells","params":[
          {"script":{"code_hash":"0x0000000000000000000000000000000000000000000000000000000000000000",
           "hash_type":"data","args":"0x01"},
           "script_type":"lock","script_search_mode":"partial"},
          "asc","0xffffffff",null],"id":'$i'}' &
done
# 11th call blocks for >=60s:
time curl -s -X POST http://localhost:8116 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_indexer_tip","params":[],"id":11}'
# Expected: response delayed ~60s; indexer block_number does not advance during window.
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L109-111)
```rust
                DBDriver::Sqlite => {
                    query_builder.and_where(format!("instr(args, ${}) > 0", param_index));
                }
```

**File:** util/rich-indexer/src/store.rs (L29-31)
```rust
pub struct SQLXPool {
    pool: Arc<OnceLock<AnyPool>>,
    pub(crate) db_driver: DBDriver,
```

**File:** util/rich-indexer/src/store.rs (L46-50)
```rust
        let mut pool_options = AnyPoolOptions::new()
            .max_connections(10)
            .min_connections(0)
            .acquire_timeout(Duration::from_secs(60))
            .max_lifetime(Duration::from_secs(1800))
```

**File:** util/rich-indexer/src/store.rs (L143-148)
```rust
    pub async fn fetch_all<'a, T>(&self, query: Query<'a, Any, T>) -> Result<Vec<AnyRow>>
    where
        T: Send + IntoArguments<'a, Any> + 'a,
    {
        let pool = self.get_pool()?;
        query.fetch_all(pool).await.map_err(Into::into)
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
