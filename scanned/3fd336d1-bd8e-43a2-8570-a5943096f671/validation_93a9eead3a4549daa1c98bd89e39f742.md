The code evidence is conclusive. Here is the assessment:

---

### Title
`get_cells_capacity` Missing `request_limit` Guard Enables O(N) RocksDB Scan Holding RPC Thread for Up to `timeout_limit` Seconds — (`util/indexer/src/service.rs`)

### Summary

`IndexerHandle::get_cells_capacity` performs an unbounded RocksDB prefix scan bounded only by a wall-clock `TimeoutIterator` (default 10 s). The sibling methods `get_cells` and `get_transactions` both enforce `request_limit` before touching the store. `get_cells_capacity` has no such guard, so any caller with RPC access can issue a broad-prefix request that holds an RPC worker thread for the full `timeout_limit` duration. Repeated parallel calls saturate the thread pool and degrade all concurrent RPC users.

### Finding Description

`IndexerHandle` stores both `request_limit` and `timeout_limit`: [1](#0-0) 

`get_cells` enforces `request_limit` before any I/O: [2](#0-1) 

`get_transactions` does the same: [3](#0-2) 

`get_cells_capacity` skips the `request_limit` check entirely and goes straight to a `TimeoutIterator`-wrapped RocksDB scan: [4](#0-3) 

The iterator runs `.take_while(|(key,_)| key.starts_with(&prefix))` with no item-count ceiling, accumulating capacity with `.sum()` until either the prefix is exhausted or the 10-second clock fires: [5](#0-4) 

A caller supplying `script.args = "0x"` with `script_search_mode = "prefix"` produces a prefix equal to just the key-type byte, matching every cell in the index. The scan reads every cell record from RocksDB plus a secondary `get(Key::OutPoint(...))` lookup per cell.

### Impact Explanation

Each such request occupies one RPC worker thread for up to 10 seconds doing heavy RocksDB I/O. Sending N parallel requests (N = thread-pool size) stalls all other RPC calls for the same window. On a large indexed chain (millions of cells) the per-request I/O load is proportional to the total cell count. This matches the stated scope: **Low (501–2000) — important performance degradation of local RPC**.

### Likelihood Explanation

The RPC is by default bound to `127.0.0.1`, so the attacker must be a local process or the operator must have exposed the RPC to a network. Infrastructure operators (exchanges, wallets, dApps) routinely expose the indexer RPC. The call requires no authentication, no special parameters, and no prior knowledge beyond the standard JSON-RPC interface documented in `rpc/src/module/indexer.rs`. [6](#0-5) 

### Recommendation

Add the same `request_limit` guard to `get_cells_capacity` that exists in `get_cells` and `get_transactions`. Since `get_cells_capacity` takes no caller-supplied `limit`, the guard should be an internal cap on the maximum number of cells scanned (e.g., abort and return an error if the scan would exceed `request_limit` items), or expose an optional `limit` parameter and enforce it identically to the other endpoints.

### Proof of Concept

```
# Saturate RPC thread pool (assume pool size = 4)
for i in $(seq 1 4); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{
      "id": '$i',
      "jsonrpc": "2.0",
      "method": "get_cells_capacity",
      "params": [{
        "script": {
          "code_hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
          "hash_type": "data",
          "args": "0x"
        },
        "script_type": "lock",
        "script_search_mode": "prefix"
      }]
    }' &
done
wait
# All 4 threads are held for ~10 s; concurrent get_cells/get_transactions calls time out
```

Assert: `get_cells_capacity` latency is O(total indexed cells); `get_cells` with `limit=1` returns in O(1). Confirm absence of `request_limit` check in `get_cells_capacity` at `util/indexer/src/service.rs:686–720`. [7](#0-6)

### Citations

**File:** util/indexer/src/service.rs (L167-172)
```rust
pub struct IndexerHandle {
    pub(crate) store: RocksdbStore,
    pub(crate) pool: Option<Arc<RwLock<Pool>>>,
    request_limit: usize,
    timeout_limit: Duration,
}
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

**File:** util/indexer/src/service.rs (L388-397)
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

**File:** util/indexer/src/service.rs (L838-839)
```rust
        if iter.is_timed_out() {
            Err(Error::invalid_params("Indexer request timeout"))
```

**File:** rpc/src/module/indexer.rs (L879-883)
```rust
    #[rpc(name = "get_cells_capacity")]
    fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>>;
```
