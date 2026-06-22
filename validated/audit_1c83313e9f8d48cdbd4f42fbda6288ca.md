### Title
Unbounded Default `request_limit` Allows RPC Callers to Exhaust Server Resources via Indexer Pagination — (`util/indexer/src/service.rs`, `util/rich-indexer/src/service.rs`)

---

### Summary

Both the CKB built-in indexer and rich-indexer expose RPC pagination endpoints (`get_cells`, `get_transactions`) that accept a `limit: Uint32` parameter. While a zero-limit guard exists, the upper-bound guard is gated on a `request_limit` field that defaults to `usize::MAX` when the operator has not explicitly configured it. This means any unprivileged RPC caller can submit `limit = 0xFFFFFFFF` (4,294,967,295) and force the node to attempt fetching billions of records from the underlying database in a single request, exhausting memory and CPU.

---

### Finding Description

`IndexerService::new()` initializes `request_limit` as:

```rust
request_limit: config.request_limit.unwrap_or(usize::MAX),
``` [1](#0-0) 

`RichIndexerService::new()` does the same:

```rust
request_limit: config.request_limit.unwrap_or(usize::MAX),
``` [2](#0-1) 

The `IndexerConfig` struct declares `request_limit: Option<usize>` and its `Default` impl sets it to `None`:

```rust
request_limit: None,
``` [3](#0-2) 

The upper-bound guard in `get_cells` (and identically in `get_transactions`) is:

```rust
let limit = limit.value(); // u32
if limit == 0 {
    return Err(Error::invalid_params("limit should be greater than 0"));
}
if limit as usize > self.request_limit {   // request_limit == usize::MAX by default
    return Err(Error::invalid_params(...));
}
``` [4](#0-3) [5](#0-4) 

On a 64-bit host, `u32::MAX as usize` (4,294,967,295) is always less than `usize::MAX` (18,446,744,073,709,551,615), so the second branch **never fires** under the default configuration. The `limit` value is then passed directly into `query_builder.limit(limit)` for the SQL-backed rich-indexer, or into `.take(limit)` over a RocksDB iterator for the classic indexer. [6](#0-5) 

The configuration comment itself acknowledges the risk but leaves the default unlimited:

```toml
# By default, there is no limitation on the size of indexer request
# However, because serde json serialization consumes too much memory(10x),
# it may cause the physical machine to become unresponsive.
# We recommend a consumption limit of 2g, which is 400 as the limit
# request_limit = 400
``` [7](#0-6) 

The classic indexer has a 10-second `TimeoutIterator` as a partial mitigation, but the rich-indexer (`AsyncRichIndexerHandle`) has no timeout field at all — only `request_limit`. [8](#0-7) 

---

### Impact Explanation

An unprivileged RPC caller submitting:

```json
{"method":"get_cells","params":[{"script":{...},"script_type":"lock"},"asc","0xffffffff",null]}
```

forces the node to issue a SQL query `LIMIT 4294967295` against the rich-indexer database (SQLite or PostgreSQL), or to iterate up to 4.3 billion RocksDB entries. Either path allocates a result `Vec` proportional to the number of matching records, serializes it to JSON (with the documented 10× memory amplification), and blocks the RPC thread for the duration. Repeated concurrent requests cause server resource exhaustion and effective denial of service of the RPC layer.

---

### Likelihood Explanation

The indexer and rich-indexer are enabled by default in many CKB node deployments. The `request_limit` option is opt-in and commented out in the shipped config template, so the vast majority of nodes run with `request_limit = usize::MAX`. The RPC endpoint is reachable by any client that can connect to the node's RPC port (default `127.0.0.1:8114`, but commonly exposed). No authentication is required.

---

### Recommendation

1. Change the default fallback from `usize::MAX` to a safe constant (e.g., `4000`) in both `IndexerService::new()` and `RichIndexerService::new()`.
2. Alternatively, make `request_limit` a `NonZeroUsize` with a bounded default so the guard always fires.
3. Add a `timeout_limit` to `AsyncRichIndexerHandle` analogous to the one in the classic indexer.

---

### Proof of Concept

```bash
# Against a default-configured CKB node with indexer enabled:
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "id": 1,
    "jsonrpc": "2.0",
    "method": "get_cells",
    "params": [
      {
        "script": {
          "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
          "hash_type": "type",
          "args": "0x"
        },
        "script_type": "lock"
      },
      "asc",
      "0xffffffff",
      null
    ]
  }'
# limit.value() = 4294967295
# request_limit  = usize::MAX (18446744073709551615)
# Guard: 4294967295 > 18446744073709551615 → false → no rejection
# SQL issued: SELECT ... LIMIT 4294967295
# Result: server attempts to allocate and serialize up to 4.3B cell records
```

### Citations

**File:** util/indexer/src/service.rs (L98-99)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
            timeout_limit: Duration::from_secs(config.timeout_limit.unwrap_or(10)),
```

**File:** util/rich-indexer/src/service.rs (L51-52)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
        }
```

**File:** util/app-config/src/configs/indexer.rs (L39-66)
```rust
    #[serde(default)]
    pub request_limit: Option<usize>,
    /// limit of indexer request timeout
    #[serde(default)]
    pub timeout_limit: Option<u64>,
    /// Rich indexer config options
    #[serde(default)]
    pub rich_indexer: RichIndexerConfig,
}

const fn default_poll_interval() -> u64 {
    2
}

impl Default for IndexerConfig {
    fn default() -> Self {
        IndexerConfig {
            poll_interval: 2,
            index_tx_pool: false,
            store: PathBuf::new(),
            secondary_path: PathBuf::new(),
            block_filter: None,
            cell_filter: None,
            db_background_jobs: None,
            db_keep_log_file_num: None,
            init_tip_hash: None,
            request_limit: None,
            timeout_limit: None,
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L156-157)
```rust
        query_builder.limit(limit);

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

**File:** resource/ckb.toml (L286-291)
```text
# # By default, there is no limitation on the size of indexer request
# # However, because serde json serialization consumes too much memory(10x),
# # it may cause the physical machine to become unresponsive.
# # We recommend a consumption limit of 2g, which is 400 as the limit,
# # which is a safer approach
# request_limit = 400
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
