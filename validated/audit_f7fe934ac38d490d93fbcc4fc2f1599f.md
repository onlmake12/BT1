The code confirms all the claims. Let me verify the exact behavior of the pool lock and the `get_cells` limit guard for completeness.

Audit Report

## Title
Unbounded Cell Iteration in `get_cells_capacity` Enables RPC-Triggered CPU Exhaustion — (File: `util/indexer/src/service.rs`)

## Summary
`IndexerHandle::get_cells_capacity` performs an unbounded RocksDB scan over all matching live cells, protected only by a wall-clock `TimeoutIterator` (default 10 s). Unlike `get_cells`, it accepts no caller-supplied `limit` and never checks `self.request_limit`. An unprivileged caller can submit a broad `search_key` to force the node to iterate all matching cells, consuming CPU and holding the tx-pool read lock for the full timeout window.

## Finding Description
`get_cells` enforces both a caller-supplied `limit` and `self.request_limit` before scanning: [1](#0-0) 

`get_cells_capacity` has neither check. After validating only the `Partial` mode exclusion, it proceeds directly to an unbounded scan: [2](#0-1) 

The `TimeoutIterator` wrapping the scan only checks wall-clock elapsed time per `next()` call — it does not cap the number of cells processed: [3](#0-2) 

The tx-pool read lock is acquired before the scan and held for its entire duration: [4](#0-3) 

The unbounded `.take_while(...).filter_map(...).sum()` chain runs until the prefix no longer matches or the timeout fires: [5](#0-4) 

Default configuration sets `request_limit = usize::MAX` and `timeout_limit = 10 s`, giving each call a 10-second uncapped scan window: [6](#0-5) 

## Impact Explanation
**Low (501–2000 points) — Important performance improvement for CKB.**

Each malicious call consumes up to 10 s of CPU on the RPC-serving thread and holds the tx-pool `RwLock` in read mode for the same duration. Rust's `std::sync::RwLock` blocks writers while any reader holds the lock; concurrent `get_cells_capacity` calls can therefore delay tx-pool write operations (transaction submission, block template generation) for the full timeout window. Sustained concurrent requests degrade RPC availability for legitimate callers. The node does not crash and network-wide consensus is not affected, placing this in the performance/availability improvement category.

## Likelihood Explanation
The indexer RPC is publicly accessible on any node with `--indexer` enabled (common for wallets, explorers, dApps). The secp256k1 lock `code_hash` is publicly known and matches tens of millions of live cells on mainnet. The attack requires only a standard JSON-RPC HTTP POST — no keys, funds, or special role. Concurrent requests (10–20 parallel calls) are trivially scripted.

## Recommendation
1. Add a server-side count limit to `get_cells_capacity`: after scanning `self.request_limit` matching cells, stop and return an error (mirroring the guard in `get_cells`).
2. Enforce a hard cell-count cap independent of wall-clock time, so fast hardware cannot be forced to scan more cells than the operator intends.
3. Consider requiring callers to supply a narrower (exact-match) `search_key` when the result set is expected to be large, or expose an optional `limit` parameter with a server-enforced maximum.

## Proof of Concept
Send 10–20 concurrent JSON-RPC requests to a node with the indexer enabled:

```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "get_cells_capacity",
  "params": [{
    "script": {
      "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
      "hash_type": "type",
      "args": "0x"
    },
    "script_type": "lock"
  }]
}
```

Each call enters `IndexerHandle::get_cells_capacity`, acquires the tx-pool read lock, and iterates all secp256k1 live cells until `timeout_limit` (10 s) expires. Concurrent calls hold the read lock simultaneously, blocking any tx-pool writer for the full window. Monitor RPC latency for other methods (e.g., `get_tip_block_number`) during the attack to confirm starvation.

### Citations

**File:** util/indexer/src/service.rs (L52-62)
```rust
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

**File:** util/indexer/src/service.rs (L721-724)
```rust
        let pool = self
            .pool
            .as_ref()
            .map(|pool| pool.read().expect("acquire lock"));
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
