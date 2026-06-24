The code confirms the claim. Let me verify the key facts:

1. `get_cells_capacity` (line 686) has **no** `request_limit` guard — confirmed at lines 686–720, compared to `get_cells` lines 212–221 and `get_transactions` lines 388–397. [1](#0-0) 
2. The iterator chain ends with `.sum()` at line 836 with **no** `.take(N)` — confirmed. [2](#0-1) 
3. `get_cells` has `.take(limit)` at line 371. [3](#0-2) 
4. `request_limit` defaults to `usize::MAX`, `timeout_limit` defaults to 10 s. [4](#0-3) 
5. `TimeoutIterator::next()` checks elapsed time cooperatively, not preemptively. [5](#0-4) 
6. The RPC trait exposes `get_cells_capacity` with no `limit` parameter and no additional guard. [6](#0-5) 

The asymmetry is real and the attack path is valid. However, the impact is correctly classified as **local RPC unresponsiveness** — the node's P2P networking and consensus continue to function; only the RPC service is affected. This maps to **Note (0–500 points): Any local RPC API crash**, not High.

---

Audit Report

## Title
Unbounded RocksDB Iteration in `get_cells_capacity` Enables RPC Thread Pool Exhaustion — (File: `util/indexer/src/service.rs`)

## Summary
`IndexerHandle::get_cells_capacity` iterates over all matching RocksDB cells bounded only by a 10-second `TimeoutIterator`, with no `request_limit` guard and no `.take(N)` early-stop. Unlike `get_cells` and `get_transactions`, which both enforce a `request_limit` rejection and a `.take(limit)` cap, `get_cells_capacity` accepts unbounded broad-prefix queries from any unprivileged RPC caller. Concurrent requests each hold a worker thread for the full timeout duration, exhausting the RPC thread pool and rendering the node's RPC unresponsive.

## Finding Description
`IndexerHandle::get_cells_capacity` (line 686) builds a RocksDB prefix iterator and folds over every matching cell with no count guard:

```rust
// line 720 — sole protection is a timeout wrapper
let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);

let capacity: u64 = iter
    .by_ref()
    .take_while(|(key, _value)| key.starts_with(&prefix))
    .filter_map(|(key, value)| { ... })
    .sum();  // line 836 — no .take(N) anywhere in this chain
```

By contrast, `get_cells` enforces both a `request_limit` rejection (lines 212–221) and a `.take(limit)` early-stop (line 371). `get_transactions` has the identical `request_limit` guard (lines 388–397). Neither protection exists in `get_cells_capacity`.

`TimeoutIterator::next()` checks elapsed time cooperatively at the top of each call (lines 55–61). A single expensive RocksDB read inside the loop body can overshoot the timeout boundary before the check fires again. `request_limit` defaults to `usize::MAX` (line 98) and `timeout_limit` defaults to 10 seconds (line 99). The RPC trait exposes `get_cells_capacity` with no `limit` parameter (lines 879–883), and the implementation passes the call directly to `IndexerHandle::get_cells_capacity` with no additional guard (lines 929–936).

## Impact Explanation
Each concurrent `get_cells_capacity` request with a broad script prefix scans every matching live cell in the indexer store, holding a worker thread for up to 10 seconds. Sustained concurrent requests saturate the RPC thread pool, causing all subsequent RPC calls to queue indefinitely. The node's P2P networking and consensus continue to function; only the RPC service is affected. This maps to **Note (0–500 points): Any local RPC API crash**.

## Likelihood Explanation
`get_cells_capacity` is part of the standard, publicly documented CKB indexer API. No authentication or privileged key is required — any process that can reach the RPC port can issue the attack. The default configuration (`request_limit` absent, `timeout_limit = 10s`) maximises exposure. The attack requires only repeated JSON-RPC HTTP POST requests, reproducible with any HTTP client.

## Recommendation
1. Add the same `request_limit` guard present in `get_cells` and `get_transactions` to `get_cells_capacity`, rejecting requests that would scan more than the configured limit.
2. Alternatively, add an internal `.take(self.request_limit)` to the iterator chain in `get_cells_capacity` so the scan stops after at most `request_limit` cells.
3. Document that `request_limit` must be configured for any publicly or semi-publicly reachable node.

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

Empty `args` prefix-matches every cell whose lock script uses this `code_hash`. Each request iterates the full matching set (bounded only by the 10-second timeout). Sending enough concurrent requests saturates the thread pool; subsequent RPC calls queue indefinitely, confirming RPC unresponsiveness.

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

**File:** util/indexer/src/service.rs (L370-372)
```rust
            })
            .take(limit)
            .collect::<Vec<_>>();
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

**File:** rpc/src/module/indexer.rs (L879-936)
```rust
    #[rpc(name = "get_cells_capacity")]
    fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>>;
}

#[derive(Clone)]
pub(crate) struct IndexerRpcImpl {
    pub(crate) handle: IndexerHandle,
}

impl IndexerRpcImpl {
    pub fn new(handle: IndexerHandle) -> Self {
        IndexerRpcImpl { handle }
    }
}

#[async_trait]
impl IndexerRpc for IndexerRpcImpl {
    fn get_indexer_tip(&self) -> Result<Option<IndexerTip>> {
        self.handle
            .get_indexer_tip()
            .map_err(|e| RPCError::custom(RPCError::Indexer, e))
    }

    fn get_cells(
        &self,
        search_key: IndexerSearchKey,
        order: IndexerOrder,
        limit: Uint32,
        after: Option<JsonBytes>,
    ) -> Result<IndexerPagination<IndexerCell>> {
        self.handle
            .get_cells(search_key, order, limit, after)
            .map_err(|e| RPCError::custom(RPCError::Indexer, e))
    }

    fn get_transactions(
        &self,
        search_key: IndexerSearchKey,
        order: IndexerOrder,
        limit: Uint32,
        after: Option<JsonBytes>,
    ) -> Result<IndexerPagination<IndexerTx>> {
        self.handle
            .get_transactions(search_key, order, limit, after)
            .map_err(|e| RPCError::custom(RPCError::Indexer, e))
    }

    fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>> {
        self.handle
            .get_cells_capacity(search_key)
            .map_err(|e| RPCError::custom(RPCError::Indexer, e))
    }
```
