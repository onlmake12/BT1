The code confirms the asymmetry. Here is the analysis:

**`get_cells` guard** (lines 216–221): [1](#0-0) 

**`get_transactions` guard** (lines 392–397): [2](#0-1) 

**`get_cells_capacity` — no `request_limit` guard, only `TimeoutIterator`**: [3](#0-2) 

The full unbounded `.sum()` loop: [4](#0-3) 

Timeout fires and returns error (the only bound): [5](#0-4) 

---

### Title
`get_cells_capacity` Missing `request_limit` Guard Enables O(N) RocksDB Scan Up to `timeout_limit` — (`util/indexer/src/service.rs`)

### Summary
`get_cells_capacity` accepts a broad `search_key` (e.g., empty `args` with `prefix` mode) and performs an unbounded full-index RocksDB scan, bounded only by `timeout_limit` (default 10 s). `get_cells` and `get_transactions` both enforce `request_limit` (a record-count cap) in addition to the timeout, but `get_cells_capacity` has no such cap.

### Finding Description
In `util/indexer/src/service.rs`, `get_cells` and `get_transactions` both validate the caller-supplied `limit` against `self.request_limit` before touching the database:

```rust
if limit > self.request_limit {
    return Err(Error::invalid_params(...));
}
```

`get_cells_capacity` takes no `limit` parameter and has no equivalent check. It constructs a `TimeoutIterator` wrapping a raw RocksDB prefix scan and calls `.sum()` over every matching cell until either the prefix boundary is reached or the 10-second timeout fires. With `args = 0x` and `script_search_mode = prefix`, the prefix is just `[KeyPrefix::CellLockScript, code_hash_byte, hash_type_byte]`, which matches every cell locked by that script type — potentially millions of entries on a large chain.

### Impact Explanation
Each such request holds an RPC handler thread (or blocks an async executor thread) performing continuous RocksDB I/O for up to 10 seconds. Sending N parallel requests saturates N threads for 10 seconds each, degrading or stalling all concurrent RPC users. The impact is local RPC performance degradation (scope: Low, 501–2000).

### Likelihood Explanation
The RPC endpoint is unauthenticated by default. Any process with network access to the RPC port (localhost or exposed) can trigger this. No key, privilege, or special state is required — only the indexer being enabled and a reasonably large indexed chain.

### Recommendation
Add the same `request_limit` guard to `get_cells_capacity`, or introduce a dedicated cap (e.g., a maximum number of cells to aggregate before returning an error). Alternatively, document that `get_cells_capacity` is intentionally unbounded and require operators to firewall the RPC port.

### Proof of Concept
```bash
# Send 20 parallel broad-prefix capacity queries
for i in $(seq 1 20); do
  curl -s -X POST http://localhost:8114 \
    -H 'Content-Type: application/json' \
    -d '{
      "id":1,"jsonrpc":"2.0","method":"get_cells_capacity",
      "params":[{
        "script":{
          "code_hash":"0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
          "hash_type":"type",
          "args":"0x"
        },
        "script_type":"lock",
        "script_search_mode":"prefix"
      }]
    }' &
done
wait
# Observe: all other RPC calls (get_tip_block_number, etc.) stall for ~10s
# Compare: get_cells with limit=1 returns in <1ms
```

### Citations

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
