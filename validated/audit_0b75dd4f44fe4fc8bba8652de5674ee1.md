I need to verify the key code claims before rendering a verdict.

All core code claims are verified against the actual source. Here is the verdict:

---

Audit Report

## Title
Missing Count-Based Scan Limit in `get_cells_capacity` Enables Full-Prefix RocksDB Scan — (`util/indexer/src/service.rs`)

## Summary
`IndexerHandle::get_cells_capacity` performs an unbounded prefix scan over all matching cells in RocksDB, bounded only by a 10-second `TimeoutIterator`. Unlike `get_cells` and `get_transactions`, which both accept a caller-supplied `limit` and terminate the iterator early with `.take(limit)`, `get_cells_capacity` accepts no limit parameter and always scans the full matching set. An unprivileged caller can issue concurrent broad-prefix requests, each holding a worker thread for the full timeout duration, making the node's RPC interface unresponsive.

## Finding Description
In `util/indexer/src/service.rs`, `get_cells_capacity` (line 686) builds a RocksDB prefix iterator and folds over every matching cell with no early-stop mechanism:

```rust
// line 720 — sole bound is a 10-second TimeoutIterator
let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);

let capacity: u64 = iter
    .by_ref()
    .take_while(|(key, _value)| key.starts_with(&prefix))
    .filter_map(...)
    .sum();  // no .take(N) anywhere in the chain
``` [1](#0-0) 

By contrast, `get_cells` enforces both a `request_limit` rejection and a `.take(limit)` early-stop:

```rust
// lines 216-221: reject oversized requests
if limit > self.request_limit {
    return Err(Error::invalid_params(...));
}
// line 371: stop the iterator after limit items
.take(limit)
``` [2](#0-1) [3](#0-2) 

`get_transactions` applies the same pattern: [4](#0-3) [5](#0-4) 

The `TimeoutIterator::next()` checks elapsed time only at the top of each call, before delegating to the inner iterator. A single expensive RocksDB read inside the loop body can overshoot the 10-second boundary: [6](#0-5) 

The default configuration sets `request_limit = usize::MAX` and `timeout_limit = 10 s`: [7](#0-6) 

`get_cells_capacity` accepts no `limit` parameter in its signature, so there is no caller-side mechanism to bound the scan: [8](#0-7) 

The RPC module at `rpc/src/module/indexer.rs` exposes this method directly with no additional guard.



## Impact Explanation
Each `get_cells_capacity` call with a broad script prefix (e.g., empty `args`) scans every matching cell for up to 10 seconds, holding a worker thread for the full duration. Flooding the RPC port with concurrent such requests saturates the thread pool, causing all subsequent RPC calls (`get_cells`, `get_block_template`, etc.) to queue indefinitely until the saturating requests time out. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash / unresponsiveness**. The evidence does not prove that P2P message processing shares the same thread pool, so the impact is bounded at the Note level.

## Likelihood Explanation
The `get_cells_capacity` RPC is part of the standard public CKB indexer API, requires no authentication, and is reachable by any HTTP client that can connect to the RPC port. The attack requires only repeated JSON-RPC POST requests with a broad script prefix. The default configuration (`request_limit = usize::MAX`, `timeout_limit = 10 s`) maximises exposure. Any node with the indexer enabled and the RPC port reachable is affected.

## Recommendation
1. Add an internal `.take(self.request_limit)` to the iterator chain in `get_cells_capacity` so the scan stops after at most `request_limit` cells, consistent with the behaviour of `get_cells` and `get_transactions`.
2. Alternatively, expose an optional `limit` parameter in the RPC signature and enforce it with both a `request_limit` rejection and a `.take(limit)` early-stop, matching the existing pattern exactly.
3. Document that `timeout_limit` should be lowered for any publicly reachable node.

## Proof of Concept
Send 20–50 concurrent HTTP POST requests to the CKB RPC port:

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

Empty `args` matches every cell whose lock script uses this `code_hash`. Each request scans the full matching set for up to 10 seconds. Subsequent RPC calls will queue indefinitely until the saturating requests time out, confirming RPC unresponsiveness.

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

**File:** util/indexer/src/service.rs (L216-221)
```rust
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

**File:** util/indexer/src/service.rs (L392-397)
```rust
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

**File:** util/indexer/src/service.rs (L686-689)
```rust
    pub fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>, Error> {
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
