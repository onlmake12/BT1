All code claims have been verified against the actual source. Here is the validation result:

---

Audit Report

## Title
Unbounded RocksDB Iteration in `get_cells_capacity` Enables RPC-Triggered DoS — (`util/indexer/src/service.rs`)

## Summary
`IndexerHandle::get_cells_capacity` iterates over all matching RocksDB cells with no count-based limit and no `.take(N)` early-stop, unlike `get_cells` and `get_transactions` which enforce both a `request_limit` rejection guard and a `.take(limit)` terminator. The sole protection is a `TimeoutIterator` defaulting to 10 seconds. An unprivileged caller can send concurrent broad-prefix requests, each holding a worker thread for the full timeout duration, saturating the RPC thread pool and making the node unresponsive.

## Finding Description
`IndexerHandle::get_cells_capacity` (line 686) builds a RocksDB prefix iterator and folds over every matching cell:

```rust
// Line 720 — only protection is a timeout wrapper
let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);

let capacity: u64 = iter
    .by_ref()
    .take_while(|(key, _value)| key.starts_with(&prefix))
    .filter_map(|(key, value)| { ... })
    .sum();
```

There is no `.take(N)` and no `request_limit` check anywhere in the function body. [1](#0-0) 

By contrast, `get_cells` enforces both:
```rust
if limit > self.request_limit {
    return Err(Error::invalid_params(...));
}
```
and terminates with `.take(limit)`. [2](#0-1) [3](#0-2) 

`get_transactions` applies the same two guards. [4](#0-3) [5](#0-4) 

`request_limit` defaults to `usize::MAX` and `timeout_limit` defaults to 10 seconds when unconfigured. [6](#0-5) 

`TimeoutIterator::next()` checks elapsed time only at the start of each call, so a single slow RocksDB read inside the loop body can overshoot the deadline non-preemptively. [7](#0-6) 

The RPC trait exposes `get_cells_capacity` with no `limit` parameter, and the `IndexerRpcImpl` implementation passes directly to `IndexerHandle::get_cells_capacity` with no additional guard. [8](#0-7) [9](#0-8) 

## Impact Explanation
Matches **High: Vulnerabilities which could easily crash a CKB node**. Concurrent `get_cells_capacity` requests with a broad script prefix (e.g., empty `args`) force the node to scan every live cell in the indexer store per request, each occupying a worker thread for up to 10 seconds. With enough concurrent requests, the thread pool is saturated, blocking all subsequent RPC calls including `send_transaction` and `get_block_template`, rendering the node functionally unresponsive. On a mainnet node with millions of live cells, a single request already performs millions of RocksDB key reads and per-cell deserializations before timeout fires.

## Likelihood Explanation
`get_cells_capacity` is part of the standard CKB indexer API, publicly documented, and reachable by any process that can connect to the RPC port. No authentication, signature, or privileged key is required. The attack requires only repeated JSON-RPC POST requests. The default configuration (`request_limit = usize::MAX`, `timeout_limit = 10s`) maximizes exposure. Any node with the RPC port reachable from a local network or the internet is directly vulnerable.

## Recommendation
1. Add a `request_limit` check to `get_cells_capacity` identical to the one in `get_cells` and `get_transactions`, rejecting requests that would scan more than the configured limit.
2. Alternatively, add an internal `.take(self.request_limit)` to the iterator chain so the scan stops after at most `request_limit` cells.
3. Lower the default `timeout_limit` and document that `request_limit` should be configured for any publicly reachable node.

## Proof of Concept
Send N concurrent HTTP POST requests to the CKB RPC port:
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
The empty `args` prefix matches every cell whose lock script uses this `code_hash`. Send 20–50 concurrent requests; observe that subsequent `get_block_template` or `send_transaction` RPC calls queue indefinitely until the timeout window expires for each batch.

### Citations

**File:** util/indexer/src/service.rs (L55-61)
```rust
    fn next(&mut self) -> Option<Self::Item> {
        if self.start_time.elapsed() > self.timeout {
            self.timed_out = true;
            return None;
        }
        self.inner.next()
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

**File:** util/indexer/src/service.rs (L371-372)
```rust
            .take(limit)
            .collect::<Vec<_>>();
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

**File:** util/indexer/src/service.rs (L674-675)
```rust
                .take(limit)
                .collect::<Vec<_>>();
```

**File:** util/indexer/src/service.rs (L720-836)
```rust
        let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
        let pool = self
            .pool
            .as_ref()
            .map(|pool| pool.read().expect("acquire lock"));

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

**File:** rpc/src/module/indexer.rs (L879-883)
```rust
    #[rpc(name = "get_cells_capacity")]
    fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>>;
```

**File:** rpc/src/module/indexer.rs (L929-936)
```rust
    fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>> {
        self.handle
            .get_cells_capacity(search_key)
            .map_err(|e| RPCError::custom(RPCError::Indexer, e))
    }
```
