The code is fully readable. Let me trace the exact parameter order mismatch.

**SQL placeholder assignment order** (`param_index` increments in `get_cells_capacity`):

1. `build_query_script_id_sql` (lines 19-23): assigns $1..$N (e.g., $1=code_hash, $2=hash_type, $3=args_lower, $4=args_upper for Prefix mode → `param_index` = 5)
2. Dead cells loop (lines 88-102): assigns $5..$4+D for each dead cell's tx_hash → `param_index` = 5+D
3. `build_cell_filter` (lines 104-109): assigns $5+D onward for filter.script / filter.output_data

**Binding order** (lines 119-178):

1. Lines 120-141: script sub-query → positions 1..N ✓
2. Lines 142-173: `filter.script` and `filter.output_data` → positions N+1, N+2, ... ✗ (SQL expects dead_cells here)
3. Lines 174-178: dead_cells tx_hashes → positions N+F+1, ... ✗ (SQL expects filter params here)

The mismatch is confirmed and concrete:

- SQL assigns dead_cells placeholders at $N+1..$N+D **before** `build_cell_filter` runs
- Binding code binds filter params at $N+1 **before** dead_cells tx_hashes

---

### Title
SQL Parameter Order Mismatch in `get_cells_capacity` Dead Cells + Filter Combined Path — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

### Summary
When `index_tx_pool=true`, the pool contains dead cells, and `filter.output_data` (or `filter.script`) is set, the SQL placeholder positions assigned for dead cells and for cell filter parameters are inverted relative to the sqlx binding sequence. This causes wrong values to be bound to wrong SQL parameters, producing either incorrect capacity results or a DB-level type error that crashes the RPC call.

### Finding Description

In `AsyncRichIndexerHandle::get_cells_capacity`, `param_index` is a shared counter that drives both SQL placeholder generation and binding order. The SQL is built in this order:

1. `build_query_script_id_sql` — consumes $1..$N
2. Dead cells `NOT IN` clause (lines 88-102) — consumes $N+1..$N+D (one per dead cell)
3. `build_cell_filter` (lines 104-109) — consumes $N+D+1 onward for `output_data`/`script` [1](#0-0) 

But the binding section does the opposite:

1. Lines 120-141: script sub-query params → $1..$N ✓
2. Lines 142-173: `filter.output_data` / `filter.script` → $N+1 onward ✗ (SQL expects dead_cells tx_hashes here)
3. Lines 174-178: dead_cells tx_hashes → $N+F+1 onward ✗ (SQL expects filter params here) [2](#0-1) 

The same structural pattern exists in `get_cells.rs` for comparison — there the dead cells block also precedes `build_cell_filter` in SQL construction but the binding section binds filter params before dead cells. [3](#0-2) 

Note that `block_range` in `build_cell_filter` embeds values as literals (not `$N` placeholders), so it does not consume any `param_index` slots and does not affect the mismatch count — the mismatch is triggered solely by `filter.output_data` or `filter.script` being set. [4](#0-3) 

### Impact Explanation

- **Wrong result**: output_data bytes (e.g., `0xdeadbeef`) are bound to the dead_cells tx_hash placeholder, and the 32-byte tx_hash is bound to the output_data placeholder. The `NOT IN` filter uses the wrong value, so dead cells are not excluded from the capacity sum — the returned capacity is inflated.
- **RPC crash**: If the DB driver enforces type or length constraints (e.g., PostgreSQL rejecting a short byte string where a 32-byte hash is expected), `sqlx` returns an error that is propagated as `Error::DB(...)` at line 190, causing the RPC to return an error response. [5](#0-4) 

### Likelihood Explanation

Requires `index_tx_pool=true` (a documented, supported production configuration), at least one pending transaction in the pool that spends an existing cell (normal operation), and a `get_cells_capacity` call with `filter.output_data` or `filter.script` set. All three conditions are routine and reachable by any unprivileged RPC caller on a node with pool indexing enabled.

### Recommendation

Reorder the binding section to match the SQL placeholder assignment order: bind dead_cells tx_hashes **before** binding filter params, mirroring the order in which `param_index` was incremented during SQL construction. Alternatively, restructure the code so that `build_cell_filter` is called (and its placeholders assigned) before the dead_cells `NOT IN` clause, and update the binding order accordingly. Adding an assertion or test that verifies `param_index` after SQL construction equals the total number of bound parameters would prevent regressions.

### Proof of Concept

1. Start a CKB node with `index_tx_pool = true` in the rich-indexer config.
2. Submit a transaction to the pool that spends an existing live cell (creating a dead cell entry in the pool).
3. Call `get_cells_capacity` with `filter.output_data` set to any non-empty value (e.g., `"0x"` for prefix match).
4. Observe: the SQL generated has dead_cells at $5/$6 and output_data at $7/$8 (for Prefix script mode), but binding puts output_data bytes at $5/$6 and the 32-byte tx_hash at $7/$8.
5. On PostgreSQL: the DB rejects the mismatched types → RPC returns a DB error. On SQLite: wrong values are compared, dead cells are not filtered, capacity result is incorrect.

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L88-109)
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

        build_cell_filter(
            self.store.db_driver,
            &mut query_builder,
            &search_key,
            &mut param_index,
        );
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L142-178)
```rust
        if let Some(filter) = search_key.filter.as_ref() {
            if let Some(script) = filter.script.as_ref() {
                query = query
                    .bind(script.code_hash.as_bytes())
                    .bind(script.hash_type as i16);
                // Default prefix search
                query = query
                    .bind(script.args.as_bytes())
                    .bind(get_binary_upper_boundary(script.args.as_bytes()))
            }
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L187-191)
```rust
        let capacity = query
            .fetch_optional(&mut *tx)
            .await
            .map_err(|err| Error::DB(err.to_string()))?
            .and_then(|row| row.try_get::<i64, _>("total_capacity").ok());
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L218-221)
```rust
        if let Some(block_range) = &filter.block_range {
            query_builder.and_where_ge("block.block_number", block_range.start());
            query_builder.and_where_lt("block.block_number", block_range.end());
        }
```
