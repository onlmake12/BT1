### Title
Unbounded Iteration Over All Indexed Cells in `get_cells_capacity()` Enables RPC-Level Denial of Service — (File: `util/indexer/src/service.rs`)

### Summary

The `get_cells_capacity()` function in the CKB indexer service iterates over every cell in the RocksDB snapshot that matches a caller-supplied script prefix, with no count-based upper bound. Unlike the sibling `get_cells()` and `get_transactions()` endpoints, which both enforce a mandatory `limit` parameter capped at `self.request_limit`, `get_cells_capacity()` accepts no `limit` at all and relies solely on a wall-clock `TimeoutIterator`. Any unprivileged RPC caller can trigger this path with a broad prefix search key, forcing the node to scan and perform a secondary RocksDB lookup for every matching cell in the entire indexed chain history, blocking the RPC handler thread for the full timeout window and enabling repeated denial-of-service.

---

### Finding Description

`get_cells()` and `get_transactions()` both validate a caller-supplied `limit` parameter before touching the iterator: [1](#0-0) 

They also terminate the iterator early with `.take(limit)`: [2](#0-1) 

`get_cells_capacity()` has no `limit` parameter in its signature and performs no count-based check: [3](#0-2) 

The function then iterates over every key that matches the prefix, and for each matching key performs an additional secondary RocksDB lookup (`snapshot.get(Key::OutPoint(...))`): [4](#0-3) 

The only guard is the `TimeoutIterator` wrapper. When the timeout fires, the function returns an error — but only after the thread has been occupied for the entire timeout window. The RPC endpoint is exposed to any caller: [5](#0-4) 

---

### Impact Explanation

An attacker who submits many transactions creating cells under a single broad lock-script prefix (or who simply uses a zero-byte prefix to match all cells) can force the indexer to scan the entire cell set on every `get_cells_capacity` call. Each scan:

1. Iterates every matching RocksDB key (potentially millions on mainnet).
2. Performs a secondary `snapshot.get()` lookup per cell.
3. Holds the RPC handler thread for the full timeout duration.

Repeated concurrent calls exhaust the RPC thread pool, making the node's RPC interface unresponsive to all other callers. This is a resource-exhaustion denial of service against the node's public RPC surface.

---

### Likelihood Explanation

The `get_cells_capacity` RPC method is publicly documented and requires no authentication. A single attacker with a standard CKB wallet can create many cells under a shared script prefix over time (normal on-chain activity), then repeatedly call `get_cells_capacity` with that prefix. No special privilege, key material, or majority hashpower is required. The attack is low-cost and repeatable.

---

### Recommendation

Apply the same `limit` enforcement pattern already used by `get_cells()` and `get_transactions()`:

1. Add a `limit: Uint32` parameter to `get_cells_capacity()` (both the trait definition in `rpc/src/module/indexer.rs` and the implementation in `util/indexer/src/service.rs`).
2. Reject requests where `limit == 0` or `limit > self.request_limit`.
3. Use `.take(limit)` on the iterator to stop after the bounded count, returning a partial sum with a cursor so callers can paginate.

Alternatively, enforce a hard maximum on the number of cells scanned (e.g., the same `self.request_limit` already configured for `get_cells`) and return an error if the result would be truncated, prompting callers to narrow their search key.

---

### Proof of Concept

1. Submit N transactions (e.g., N = 500,000) each creating one cell with `lock.args = 0x` (empty args, matching any prefix search).
2. Wait for the indexer to sync.
3. Call `get_cells_capacity` with `search_key.script = { code_hash: <any>, hash_type: "type", args: "0x" }` and `script_search_mode: "prefix"`.
4. Observe that the RPC thread iterates all N cells, performing N secondary RocksDB lookups, and blocks for the full `timeout_limit` window before returning an error.
5. Send concurrent requests to saturate the RPC thread pool; all other RPC calls time out.

The contrast with `get_cells` is direct: the same search key sent to `get_cells` with `limit: 1` returns immediately after one cell is found, while `get_cells_capacity` with the same key scans the entire matching set. [6](#0-5) [7](#0-6)

### Citations

**File:** util/indexer/src/service.rs (L200-221)
```rust
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

**File:** util/indexer/src/service.rs (L686-752)
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
