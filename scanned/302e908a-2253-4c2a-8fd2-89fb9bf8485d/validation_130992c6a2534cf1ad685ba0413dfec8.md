### Title
Unbounded RocksDB Iteration in `get_cells_capacity` Enables RPC-Triggered CPU/IO Exhaustion — (`util/indexer/src/service.rs`)

---

### Summary

The `IndexerHandle::get_cells_capacity` RPC handler iterates over all matching cells in RocksDB without any count-based limit. Unlike `get_cells` and `get_transactions`, which enforce a `request_limit` guard and a `.take(limit)` early-stop, `get_cells_capacity` only wraps the iterator in a `TimeoutIterator` (default 10 seconds). An unprivileged RPC caller can send concurrent broad-prefix requests, each holding a worker thread for the full timeout duration, exhausting the async thread pool and causing the node to become unresponsive.

---

### Finding Description

In `util/indexer/src/service.rs`, `IndexerHandle::get_cells_capacity` builds a RocksDB prefix iterator and folds over every matching cell to sum capacities:

```rust
// Line 720 — only protection is a timeout wrapper
let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);

let capacity: u64 = iter
    .by_ref()
    .take_while(|(key, _value)| key.starts_with(&prefix))
    .filter_map(|(key, value)| { ... })
    .sum();
``` [1](#0-0) 

There is no `request_limit` check and no `.take(N)` call. Compare this to `get_cells`, which enforces both:

```rust
// Lines 212-221 — request_limit guard present
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
``` [2](#0-1) 

And `get_cells` also stops early with `.take(limit)`: [3](#0-2) 

The same `request_limit` guard is present in `get_transactions`: [4](#0-3) 

The `TimeoutIterator` is the sole bound on `get_cells_capacity`. Its timeout defaults to 10 seconds when `timeout_limit` is not configured: [5](#0-4) 

The config comment explicitly warns that `request_limit` is absent by default and that large responses can make the machine unresponsive: [6](#0-5) 

The `TimeoutIterator` checks elapsed time only at the start of each `next()` call: [7](#0-6) 

This means a single expensive RocksDB read inside the loop body can still block beyond the timeout boundary, and the check is not preemptive.

The RPC trait exposes `get_cells_capacity` with no `limit` parameter: [8](#0-7) 

The implementation passes the call directly to `IndexerHandle::get_cells_capacity` with no additional guard: [9](#0-8) 

---

### Impact Explanation

An RPC caller submitting concurrent `get_cells_capacity` requests with a broad script prefix (e.g., empty `args`) forces the node to scan every live cell in the indexer store for each request. Each request occupies a Tokio worker thread (via `block_in_place` or the synchronous RPC executor) for up to `timeout_limit` seconds. With enough concurrent requests, the thread pool is saturated, blocking all other RPC calls and potentially delaying P2P message processing that shares the async runtime. On a mature mainnet node with millions of live cells, a single request already performs millions of RocksDB key reads and per-cell deserialization steps before the timeout fires.

---

### Likelihood Explanation

The `get_cells_capacity` RPC is part of the standard CKB indexer API, documented publicly, and callable by any process that can reach the RPC port. Nodes that expose the RPC to a local network, a shared server, or (misconfigured) to the public internet are directly reachable. No authentication, signature, or privileged key is required. The attack requires only repeated JSON-RPC POST requests, which any HTTP client can issue. The default configuration (`request_limit` absent, `timeout_limit = 10s`) maximises exposure.

---

### Recommendation

1. Add a `request_limit` check to `get_cells_capacity` identical to the one in `get_cells` and `get_transactions`, rejecting requests that would scan more than the configured limit.
2. Alternatively, add an internal `.take(self.request_limit)` to the iterator chain so the scan stops after at most `request_limit` cells, returning a partial result or an error.
3. Consider lowering the default `timeout_limit` and documenting that `request_limit` should be set for any publicly reachable node.

---

### Proof of Concept

Send `N` concurrent HTTP POST requests to the CKB RPC port:

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

The empty `args` prefix matches every cell whose lock script uses this `code_hash`, which on mainnet covers the majority of all live cells. Each request iterates the full matching set (bounded only by the 10-second timeout), holding a worker thread for the duration. Sending 20–50 concurrent requests saturates the thread pool, causing subsequent RPC calls (including `send_transaction` and `get_block_template`) to queue indefinitely.

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

**File:** util/indexer/src/service.rs (L93-100)
```rust
        Self {
            store,
            sync,
            block_filter: config.block_filter.clone(),
            cell_filter: config.cell_filter.clone(),
            request_limit: config.request_limit.unwrap_or(usize::MAX),
            timeout_limit: Duration::from_secs(config.timeout_limit.unwrap_or(10)),
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

**File:** util/indexer/src/service.rs (L339-372)
```rust
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

                last_key = key.to_vec();

                Some(IndexerCell {
                    output: output.into(),
                    output_data: if filter_options.with_data {
                        Some(output_data.into())
                    } else {
                        None
                    },
                    out_point: out_point.into(),
                    block_number: block_number.into(),
                    tx_index: tx_index.into(),
                })
            })
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

**File:** util/indexer/src/service.rs (L718-728)
```rust
        let mode = IteratorMode::From(from_key.as_ref(), direction);
        let snapshot = self.store.inner().snapshot();
        let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
        let pool = self
            .pool
            .as_ref()
            .map(|pool| pool.read().expect("acquire lock"));

        let capacity: u64 = iter
            .by_ref()
            .take_while(|(key, _value)| key.starts_with(&prefix))
```

**File:** resource/ckb.toml (L286-293)
```text
# # By default, there is no limitation on the size of indexer request
# # However, because serde json serialization consumes too much memory(10x),
# # it may cause the physical machine to become unresponsive.
# # We recommend a consumption limit of 2g, which is 400 as the limit,
# # which is a safer approach
# request_limit = 400
# # By default, there is a timeout limit of 10 seconds for each indexer request
# timeout_limit = 10
```

**File:** rpc/src/module/indexer.rs (L879-884)
```rust
    #[rpc(name = "get_cells_capacity")]
    fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>>;
}
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
