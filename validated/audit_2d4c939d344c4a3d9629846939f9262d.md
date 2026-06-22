### Title
Unbounded Cell Iteration in `get_cells_capacity` Enables RPC-Triggered CPU Exhaustion DoS — (File: `util/indexer/src/service.rs`)

---

### Summary

The `get_cells_capacity` RPC endpoint in `IndexerHandle` iterates over **all** matching live cells in the RocksDB store without any count limit. Unlike `get_cells` and `get_transactions`, which enforce a caller-supplied `limit` and a server-configured `request_limit`, `get_cells_capacity` accepts no `limit` parameter and never checks `self.request_limit`. The only protection is a wall-clock `TimeoutIterator` (default 10 seconds). An unprivileged RPC caller can submit a broad prefix `search_key` matching millions of cells, forcing the node to iterate over all of them in a single synchronous call, consuming CPU and holding the tx-pool read lock for the full timeout duration. Concurrent requests amplify the impact into sustained RPC thread starvation.

---

### Finding Description

**Root cause — `get_cells_capacity` has no count limit:**

`get_cells` enforces both a caller-supplied `limit` and `self.request_limit`:

```rust
// util/indexer/src/service.rs  lines 212-221
let limit = limit.value() as usize;
if limit == 0 {
    return Err(Error::invalid_params("limit should be greater than 0"));
}
if limit > self.request_limit {
    return Err(Error::invalid_params(...));
}
```

`get_cells_capacity` has **neither**. It accepts only a `search_key` with no `limit` argument, and its body goes directly to an unbounded RocksDB scan:

```rust
// util/indexer/src/service.rs  lines 686-836
pub fn get_cells_capacity(
    &self,
    search_key: IndexerSearchKey,
) -> Result<Option<IndexerCellsCapacity>, Error> {
    // ... no limit check, no request_limit check ...
    let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
    let capacity: u64 = iter
        .by_ref()
        .take_while(|(key, _value)| key.starts_with(&prefix))
        .filter_map(|(key, value)| { /* per-cell work */ })
        .sum();
```

The `TimeoutIterator` stops iteration after `timeout_limit` elapses, but it does not prevent the work from happening — it only caps how long it runs:

```rust
// util/indexer/src/service.rs  lines 52-61
fn next(&mut self) -> Option<Self::Item> {
    if self.start_time.elapsed() > self.timeout {
        self.timed_out = true;
        return None;
    }
    self.inner.next()
}
```

**Default configuration makes the window large:**

```rust
// util/indexer/src/service.rs  lines 98-99
request_limit: config.request_limit.unwrap_or(usize::MAX),
timeout_limit: Duration::from_secs(config.timeout_limit.unwrap_or(10)),
```

With the default `timeout_limit` of 10 seconds and `request_limit` of `usize::MAX`, a single `get_cells_capacity` call can consume 10 full seconds of CPU while holding the tx-pool read lock:

```rust
// util/indexer/src/service.rs  lines 721-724
let pool = self
    .pool
    .as_ref()
    .map(|pool| pool.read().expect("acquire lock"));
```

**Attacker-controlled amplification via prefix search:**

The default `script_search_mode` is `Prefix`. An attacker supplies a `search_key` with a popular `code_hash` (e.g., the secp256k1 lock) and an empty or short `args`, which matches every cell using that script. On a mainnet node with millions of such cells, the iterator scans all of them until the timeout fires.

The RPC entry point is:

```rust
// rpc/src/module/indexer.rs  lines 879-883
#[rpc(name = "get_cells_capacity")]
fn get_cells_capacity(
    &self,
    search_key: IndexerSearchKey,
) -> Result<Option<IndexerCellsCapacity>>;
```

No authentication or rate-limiting is applied before reaching `IndexerHandle::get_cells_capacity`.

---

### Impact Explanation

**Impact: Medium**

Each malicious `get_cells_capacity` call:
1. Consumes up to `timeout_limit` (default 10 s) of CPU on the RPC-serving thread.
2. Holds the tx-pool `RwLock` in read mode for the same duration, potentially blocking writers (tx submission, block processing notifications).
3. Holds a RocksDB snapshot, increasing memory pressure.

A single attacker sending concurrent requests (e.g., 10–20 parallel calls) can saturate the node's RPC thread pool for the full timeout window, causing legitimate RPC calls to queue or time out. This degrades node availability without requiring any privileged access or on-chain funds.

---

### Likelihood Explanation

**Likelihood: Medium**

- The indexer RPC is publicly accessible on any node that enables `--indexer`.
- The secp256k1 lock script has tens of millions of live cells on CKB mainnet; its `code_hash` is publicly known.
- The attack requires only a standard JSON-RPC HTTP request — no keys, no funds, no special role.
- The only friction is that the node must have the indexer enabled, which is common for infrastructure nodes (wallets, explorers, dApps).

---

### Recommendation

1. **Add a server-side count limit to `get_cells_capacity`**: After accumulating `self.request_limit` matching cells, stop iteration and return the partial result (or an error). Mirror the guard already present in `get_cells`.
2. **Enforce a hard cap on the number of cells scanned** (not just wall-clock time), so that a fast machine cannot be forced to scan more cells than the operator intends.
3. **Consider rate-limiting** the `get_cells_capacity` endpoint at the RPC layer, or requiring callers to supply a narrower (exact-match) `search_key` when the result set is expected to be large.

---

### Proof of Concept

Send the following JSON-RPC request to a CKB node with the indexer enabled. Use the secp256k1 lock `code_hash` with empty `args` (prefix mode matches all secp256k1 cells):

```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "get_cells_capacity",
  "params": [
    {
      "script": {
        "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
        "hash_type": "type",
        "args": "0x"
      },
      "script_type": "lock"
    }
  ]
}
```

Send 10–20 such requests concurrently. Each will cause `IndexerHandle::get_cells_capacity` to iterate over all secp256k1 live cells (tens of millions on mainnet) until `timeout_limit` (10 s) expires. The node's RPC thread pool is saturated for the duration, blocking all other RPC callers.

**Code path:**

`get_cells_capacity` RPC handler [1](#0-0) 

→ `IndexerHandle::get_cells_capacity` — no limit check, unbounded scan [2](#0-1) 

→ `TimeoutIterator` — only wall-clock protection, no count cap [3](#0-2) 

→ Unbounded `.take_while(...).filter_map(...).sum()` over all matching cells [4](#0-3) 

Contrast with `get_cells`, which enforces both `limit` and `request_limit`: [5](#0-4) 

Default configuration — `request_limit = usize::MAX`, `timeout_limit = 10 s`: [6](#0-5)

### Citations

**File:** rpc/src/module/indexer.rs (L879-883)
```rust
    #[rpc(name = "get_cells_capacity")]
    fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>>;
```

**File:** util/indexer/src/service.rs (L30-62)
```rust
struct TimeoutIterator<I> {
    inner: I,
    start_time: Instant,
    timeout: Duration,
    timed_out: bool,
}

impl<I> TimeoutIterator<I> {
    fn new(inner: I, timeout: Duration) -> Self {
        Self {
            inner,
            start_time: Instant::now(),
            timeout,
            timed_out: false,
        }
    }

    fn is_timed_out(&self) -> bool {
        self.timed_out
    }
}

impl<I: Iterator> Iterator for TimeoutIterator<I> {
    type Item = I::Item;

    fn next(&mut self) -> Option<Self::Item> {
        if self.start_time.elapsed() > self.timeout {
            self.timed_out = true;
            return None;
        }
        self.inner.next()
    }
}
```

**File:** util/indexer/src/service.rs (L98-99)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
            timeout_limit: Duration::from_secs(config.timeout_limit.unwrap_or(10)),
```

**File:** util/indexer/src/service.rs (L212-221)
```rust
        let limit = limit.value() as usize;
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/indexer/src/service.rs (L686-720)
```rust
    pub fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>, Error> {
        if search_key
            .script_search_mode
            .as_ref()
            .map(|mode| *mode == IndexerSearchMode::Partial)
            .unwrap_or(false)
        {
            return Err(Error::invalid_params(
                "the CKB indexer doesn't support search_key.script_search_mode partial search mode, \
                please use the CKB rich-indexer for such search",
            ));
        }

        let (prefix, from_key, direction, skip) = build_query_options(
            &search_key,
            KeyPrefix::CellLockScript,
            KeyPrefix::CellTypeScript,
            IndexerOrder::Asc,
            None,
        )?;
        let filter_script_type = match search_key.script_type {
            IndexerScriptType::Lock => IndexerScriptType::Type,
            IndexerScriptType::Type => IndexerScriptType::Lock,
        };
        let script_search_exact = matches!(
            search_key.script_search_mode,
            Some(IndexerSearchMode::Exact)
        );
        let filter_options: FilterOptions = search_key.try_into()?;
        let mode = IteratorMode::From(from_key.as_ref(), direction);
        let snapshot = self.store.inner().snapshot();
        let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
```

**File:** util/indexer/src/service.rs (L726-836)
```rust
        let capacity: u64 = iter
            .by_ref()
            .take_while(|(key, _value)| key.starts_with(&prefix))
            .filter_map(|(key, value)| {
                if script_search_exact {
                    // Exact match mode, check key length is equal to full script len + BlockNumber (8) + TxIndex (4) + OutputIndex (4)
                    if key.len() != prefix.len() + 16 {
                        return None;
                    }
                }
                let tx_hash = packed::Byte32::from_slice(value.as_ref()).expect("stored tx hash");
                let index =
                    u32::from_be_bytes(key[key.len() - 4..].try_into().expect("stored index"));
                let out_point = packed::OutPoint::new(tx_hash, index);
                if pool
                    .as_ref()
                    .map(|pool| pool.is_consumed_by_pool_tx(&out_point))
                    .unwrap_or_default()
                {
                    return None;
                }
                let (block_number, _tx_index, output, output_data) = Value::parse_cell_value(
                    &snapshot
                        .get(Key::OutPoint(&out_point).into_vec())
                        .expect("get OutPoint should be OK")
                        .expect("stored OutPoint"),
                );

                if let Some(prefix) = filter_options.script_prefix.as_ref() {
                    match filter_script_type {
                        IndexerScriptType::Lock => {
                            if !extract_raw_data(&output.lock())
                                .as_slice()
                                .starts_with(prefix)
                            {
                                return None;
                            }
                        }
                        IndexerScriptType::Type => {
                            if output.type_().is_none()
                                || !extract_raw_data(&output.type_().to_opt().unwrap())
                                    .as_slice()
                                    .starts_with(prefix)
                            {
                                return None;
                            }
                        }
                    }
                }

                if let Some([r0, r1]) = filter_options.script_len_range {
                    match filter_script_type {
                        IndexerScriptType::Lock => {
                            let script_len = extract_raw_data(&output.lock()).len();
                            if script_len < r0 || script_len > r1 {
                                return None;
                            }
                        }
                        IndexerScriptType::Type => {
                            let script_len = output
                                .type_()
                                .to_opt()
                                .map(|script| extract_raw_data(&script).len())
                                .unwrap_or_default();
                            if script_len < r0 || script_len > r1 {
                                return None;
                            }
                        }
                    }
                }

                if let Some((data, mode)) = &filter_options.output_data {
                    match mode {
                        IndexerSearchMode::Prefix => {
                            if !output_data.raw_data().starts_with(data) {
                                return None;
                            }
                        }
                        IndexerSearchMode::Exact => {
                            if output_data.raw_data() != data {
                                return None;
                            }
                        }
                        IndexerSearchMode::Partial => {
                            memmem::find(&output_data.raw_data(), data)?;
                        }
                    }
                }

                if let Some([r0, r1]) = filter_options.output_data_len_range
                    && (output_data.len() < r0 || output_data.len() >= r1)
                {
                    return None;
                }

                if let Some([r0, r1]) = filter_options.output_capacity_range {
                    let capacity: core::Capacity = output.capacity().into();
                    if capacity < r0 || capacity >= r1 {
                        return None;
                    }
                }

                if let Some([r0, r1]) = filter_options.block_range
                    && (block_number < r0 || block_number >= r1)
                {
                    return None;
                }

                Some(Into::<core::Capacity>::into(output.capacity()).as_u64())
            })
            .sum();
```
