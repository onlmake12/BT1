The claimed parameter binding order mismatch is **real and confirmed** in the code. Here is the precise trace:

---

### Title
Parameter Binding Order Mismatch in `get_cells_capacity` Causes Silent Wrong Capacity Results When Pool Filter and `output_data` Filter Are Both Active — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

---

### Summary

In `AsyncRichIndexerHandle::get_cells_capacity`, the SQL placeholder numbering assigns dead-cell `tx_hash` slots **before** `output_data` filter slots, but the binding code binds `output_data` bytes **before** dead-cell `tx_hash` bytes. When both conditions are active simultaneously (pool non-empty → `dead_cells` non-empty, and `filter.output_data` set), every parameter from position N onward is swapped, producing silently wrong capacity sums.

---

### Finding Description

**SQL-building phase** (`param_index` starts at 1, Prefix mode shown): [1](#0-0) 

After `build_query_script_id_sql` returns, `param_index = 5` (positions 1–4 consumed by `code_hash`, `hash_type`, `args` lower/upper).

Dead-cell placeholders are then stamped **next**: [2](#0-1) 

For one dead cell this consumes `$5` for `tx_hash[0]`, leaving `param_index = 6`.

`build_cell_filter` is then called and stamps `output_data` filter placeholders starting at `$6`: [3](#0-2) 

So the SQL expects: `$5` = `tx_hash[0]`, `$6` = `output_data` lower bound, `$7` = `output_data` upper bound.

**Binding phase** — filter params are bound **before** dead-cell hashes: [4](#0-3) 

The binding sequence delivers:
- position 5 → `output_data` bytes (SQL expects `tx_hash`)
- position 6 → `output_data` upper boundary (SQL expects `output_data` lower)
- position 7 → `tx_hash[0]` bytes (SQL expects `output_data` upper)

The identical mismatch exists in `get_cells.rs`: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

- The `NOT IN (tx_hash, output_index)` dead-cell exclusion clause receives `output_data` bytes as the `tx_hash` comparand. Since those bytes will never match any real `tx_hash`, **dead cells are not excluded** — the capacity sum silently over-counts by including cells that a pending pool transaction has already consumed.
- The `output.data` filter clause receives `tx_hash` bytes as the data comparand, so it filters against the wrong value — cells are silently included or excluded based on garbage criteria.

The result is a **silent wrong capacity value** returned to the RPC caller, with no error surfaced.

---

### Likelihood Explanation

Preconditions are ordinary production conditions: `index_tx_pool = true` (a supported configuration), at least one pending transaction in the mempool (routine), and a caller supplying `filter.output_data` (a documented, publicly accessible parameter). No privilege is required. The existing tests that exercise `output_data` filtering all use `pool = None`, so the mismatch is never exercised by the test suite. [7](#0-6) [8](#0-7) 

(The pool-overlay test at line 1136 does not set `filter.output_data`, so the swap is never triggered.)

---

### Recommendation

Bind dead-cell `tx_hash` values **immediately after** the script params and **before** any filter params, mirroring the SQL placeholder order. Alternatively, restructure `build_cell_filter` to be called before the dead-cell placeholder loop so that both phases stay in sync.

---

### Proof of Concept

1. Start a rich-indexer node with `index_tx_pool = true`.
2. Submit a pending transaction spending any live cell (so `dead_cells` is non-empty).
3. Call `get_cells_capacity` with `filter.output_data` set to any non-empty value.
4. Call `get_cells_capacity` again with the same `search_key` but `filter = null` (no output_data filter, no pool interaction).
5. Subtract the expected dead-cell capacity from result (4). The two results should differ only by the dead-cell capacity; instead, result (3) will include the dead cell's capacity (exclusion silently failed) and will apply the wrong data filter, producing a demonstrably incorrect sum.

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L130-139)
```rust
        .and_where_eq("code_hash", format!("${}", param_index));
    *param_index += 1;
    query_builder.and_where_eq("hash_type", format!("${}", param_index));
    *param_index += 1;
    match script_search_mode {
        Some(IndexerSearchMode::Prefix) | None => {
            query_builder.and_where_ge("args", format!("${}", param_index));
            *param_index += 1;
            query_builder.and_where_lt("args", format!("${}", param_index));
            *param_index += 1;
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L222-229)
```rust
        if filter.output_data.is_some() {
            match filter.output_data_filter_mode {
                Some(IndexerSearchMode::Prefix) | None => {
                    query_builder.and_where_ge("output.data", format!("${}", param_index));
                    *param_index += 1;
                    query_builder.and_where_lt("output.data", format!("${}", param_index));
                    *param_index += 1;
                }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L88-102)
```rust
        if !dead_cells.is_empty() {
            let placeholders = dead_cells
                .iter()
                .map(|(_, output_index)| {
                    let placeholder = format!("(${}, {})", param_index, output_index);
                    param_index += 1;
                    placeholder
                })
                .collect::<Vec<_>>()
                .join(",");
            query_builder.and_where(format!(
                "(ckb_transaction.tx_hash, output_index) NOT IN ({})",
                placeholders
            ));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L152-178)
```rust
            if let Some(data) = &filter.output_data {
                match &filter.output_data_filter_mode {
                    Some(IndexerSearchMode::Prefix) | None => {
                        query = query
                            .bind(data.as_bytes())
                            .bind(get_binary_upper_boundary(data.as_bytes()));
                    }
                    Some(IndexerSearchMode::Exact) => {
                        query = query.bind(data.as_bytes());
                    }
                    Some(IndexerSearchMode::Partial) => match self.store.db_driver {
                        DBDriver::Postgres => {
                            let new_data = escape_and_wrap_for_postgres_like(data);
                            query = query.bind(new_data);
                        }
                        DBDriver::Sqlite => {
                            query = query.bind(data.as_bytes());
                        }
                    },
                }
            }
        }
        if !dead_cells.is_empty() {
            for (tx_hash, _) in dead_cells {
                query = query.bind(tx_hash)
            }
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L124-135)
```rust
        if !dead_cells.is_empty() {
            let placeholders = dead_cells
                .iter()
                .map(|(_, output_index)| {
                    let placeholder = format!("(${}, {})", param_index, output_index);
                    param_index += 1;
                    placeholder
                })
                .collect::<Vec<_>>()
                .join(",");
            query_builder.and_where(format!("(tx_hash, output_index) NOT IN ({})", placeholders));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L199-225)
```rust
            if let Some(data) = &filter.output_data {
                match &filter.output_data_filter_mode {
                    Some(IndexerSearchMode::Prefix) | None => {
                        query = query
                            .bind(data.as_bytes())
                            .bind(get_binary_upper_boundary(data.as_bytes()));
                    }
                    Some(IndexerSearchMode::Exact) => {
                        query = query.bind(data.as_bytes());
                    }
                    Some(IndexerSearchMode::Partial) => match self.store.db_driver {
                        DBDriver::Postgres => {
                            let new_data = escape_and_wrap_for_postgres_like(data);
                            query = query.bind(new_data);
                        }
                        DBDriver::Sqlite => {
                            query = query.bind(data.as_bytes());
                        }
                    },
                }
            }
        }
        if !dead_cells.is_empty() {
            for (tx_hash, _) in dead_cells {
                query = query.bind(tx_hash)
            }
        }
```

**File:** util/rich-indexer/src/tests/query.rs (L476-478)
```rust
async fn get_cells_capacity() {
    let pool = connect_sqlite(MEMORY_DB).await;
    let indexer = AsyncRichIndexerHandle::new(pool.clone(), None, usize::MAX);
```

**File:** util/rich-indexer/src/tests/query.rs (L1136-1150)
```rust
    // test get_cells_capacity rpc with tx-pool overlay
    let capacity = rpc
        .get_cells_capacity(IndexerSearchKey {
            script: lock_script1.into(),
            ..Default::default()
        })
        .await
        .unwrap()
        .unwrap();

    assert_eq!(
        1000 * 100000000 * total_blocks,
        capacity.capacity.value(),
        "cellbases (last block live cell was consumed by a pending tx in the pool)"
    );
```
