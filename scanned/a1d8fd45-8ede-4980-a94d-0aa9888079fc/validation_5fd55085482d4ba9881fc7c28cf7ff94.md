The bug is real and confirmed by direct code inspection. Here is the full analysis:

---

### Title
SQL Placeholder/Bind Order Mismatch in `get_cells` and `get_cells_capacity` Causes Wrong Results or DB Error When Dead Cells and Filter Params Coexist — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`)

### Summary

When `index_tx_pool=true`, the tx-pool has at least one pending transaction (making `dead_cells` non-empty), and the RPC caller supplies a `filter.script` or `filter.output_data`, the SQL placeholder numbering and the sqlx `.bind()` call sequence are in opposite order for dead-cell tx_hashes vs. filter parameters. Wrong values are bound to wrong `$N` positions, producing either silently incorrect query results or a DB driver error.

### Finding Description

**SQL-building phase** (`get_cells`, lines 36–150):

1. `build_query_script_sql` starts at `param_index=1` and consumes $1–$4 (prefix mode). After: `param_index=5`. [1](#0-0) 

2. The dead-cells loop assigns `$5`, `$6`, …, `$5+N-1` (one placeholder per dead cell tx_hash). After: `param_index=5+N`. [2](#0-1) 

3. `build_cell_filter` is called next and assigns `$5+N`, `$5+N+1`, … to filter params (4 params for `filter.script`, 1–2 for `filter.output_data`). [3](#0-2) [4](#0-3) 

**Bind phase** (lines 165–225) — the order is **reversed**:

- First, script params are bound ($1–$4). ✓
- Then, **filter params** are bound — these land at positions $5, $6, … but the SQL expects **dead-cell tx_hashes** there. [5](#0-4) 
- Finally, **dead-cell tx_hashes** are bound — these land at positions $5+filter_count, … but the SQL expects **filter params** there. [6](#0-5) 

**Concrete example** (N=1 dead cell, `filter.script` set, prefix mode):

| Position | SQL expects | Bind provides |
|---|---|---|
| $5 | `dead_cell[0].tx_hash` (32-byte blob) | `filter.script.code_hash` (32-byte blob) |
| $6 | `filter.script.code_hash` | `filter.script.hash_type` (i16) |
| $7 | `filter.script.hash_type` | `filter.script.args` (bytes) |
| $8 | `filter.script.args` (lower) | `upper_boundary(filter.script.args)` |
| $9 | `upper_boundary(filter.script.args)` | `dead_cell[0].tx_hash` |

The same structural bug is present in `get_cells_capacity`. [7](#0-6) 

### Impact Explanation

- **Silent wrong results**: The dead-cell exclusion filter compares a script code_hash blob against `tx_hash`, so cells that are actually spent in the pool are not excluded and are returned as live. Conversely, the filter script comparison receives wrong values, so the filter either matches everything or nothing.
- **DB driver error / RPC crash**: On PostgreSQL, binding an `i16` (hash_type) to a `BYTEA` column position causes a type error, propagating as an `Error::DB(...)` returned to the RPC caller.

### Likelihood Explanation

Preconditions are all reachable in normal production operation:
- `index_tx_pool=true` is a supported, documented configuration.
- Any pending transaction in the pool creates at least one dead cell.
- Any unprivileged caller of `ckb_get_cells` can supply `filter.script`.

No special privileges, keys, or majority hashpower are required.

### Recommendation

In the bind phase, bind dead-cell tx_hashes **before** filter params, matching the SQL placeholder assignment order. Alternatively, restructure `build_cell_filter` to be called before the dead-cells loop so both phases are consistent. The same fix must be applied to `get_cells_capacity`.

### Proof of Concept

```
SQL placeholder order (N=1 dead cell, filter.script set, prefix mode):
  $1 = script.code_hash
  $2 = script.hash_type
  $3 = script.args (lower)
  $4 = script.args (upper)
  $5 = dead_cell[0].tx_hash        ← assigned by dead-cells loop
  $6 = filter.script.code_hash     ← assigned by build_cell_filter
  $7 = filter.script.hash_type
  $8 = filter.script.args (lower)
  $9 = filter.script.args (upper)

Bind sequence:
  .bind(script.code_hash)           → $1 ✓
  .bind(script.hash_type)           → $2 ✓
  .bind(script.args)                → $3 ✓
  .bind(upper_boundary(args))       → $4 ✓
  .bind(filter.script.code_hash)    → $5 ✗ (SQL expects dead_cell tx_hash)
  .bind(filter.script.hash_type)    → $6 ✗ (SQL expects filter.script.code_hash)
  .bind(filter.script.args)         → $7 ✗
  .bind(upper_boundary(f.args))     → $8 ✗
  .bind(dead_cell[0].tx_hash)       → $9 ✗ (SQL expects filter.script.args upper)

Result: DB error (type mismatch on PostgreSQL) or silent wrong filtering (SQLite).
```

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L78-119)
```rust
fn build_query_script_sql(
    db_driver: DBDriver,
    script_search_mode: &Option<IndexerSearchMode>,
    param_index: &mut usize,
) -> Result<String, Error> {
    let mut query_builder = SqlBuilder::select_from("script");
    query_builder
        .field("script.id")
        .field("script.code_hash")
        .field("script.hash_type")
        .field("script.args")
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
        }
        Some(IndexerSearchMode::Exact) => {
            query_builder.and_where_eq("args", format!("${}", param_index));
            *param_index += 1;
        }
        Some(IndexerSearchMode::Partial) => {
            match db_driver {
                DBDriver::Postgres => {
                    query_builder.and_where(format!("args LIKE ${}", param_index));
                }
                DBDriver::Sqlite => {
                    query_builder.and_where(format!("instr(args, ${}) > 0", param_index));
                }
            }
            *param_index += 1;
        }
    }
    let sql_sub_query = query_builder
        .subquery()
        .map_err(|err| Error::DB(err.to_string()))?;
    Ok(sql_sub_query)
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L163-248)
```rust
fn build_cell_filter(
    db_driver: DBDriver,
    query_builder: &mut SqlBuilder,
    search_key: &IndexerSearchKey,
    param_index: &mut usize,
) {
    let filter = convert_max_values_in_search_filter(&search_key.filter);

    if let Some(ref filter) = filter {
        if filter.script.is_some() {
            match search_key.script_type {
                IndexerScriptType::Lock => {
                    query_builder
                        .and_where_eq("type_script.code_hash", format!("${}", param_index));
                    *param_index += 1;
                    query_builder
                        .and_where_eq("type_script.hash_type", format!("${}", param_index));
                    *param_index += 1;
                    query_builder.and_where_ge("type_script.args", format!("${}", param_index));
                    *param_index += 1;
                    query_builder.and_where_lt("type_script.args", format!("${}", param_index));
                    *param_index += 1;
                }
                IndexerScriptType::Type => {
                    query_builder
                        .and_where_eq("lock_script.code_hash", format!("${}", param_index));
                    *param_index += 1;
                    query_builder
                        .and_where_eq("lock_script.hash_type", format!("${}", param_index));
                    *param_index += 1;
                    query_builder.and_where_ge("lock_script.args", format!("${}", param_index));
                    *param_index += 1;
                    query_builder.and_where_lt("lock_script.args", format!("${}", param_index));
                    *param_index += 1;
                }
            }
        }
        if let Some(script_len_range) = &filter.script_len_range {
            match search_key.script_type {
                IndexerScriptType::Lock => {
                    add_filter_script_len_range_conditions(query_builder, "type", script_len_range);
                }
                IndexerScriptType::Type => {
                    add_filter_script_len_range_conditions(query_builder, "lock", script_len_range);
                }
            }
        }
        if let Some(data_len_range) = &filter.output_data_len_range {
            query_builder.and_where_ge("LENGTH(output.data)", data_len_range.start());
            query_builder.and_where_lt("LENGTH(output.data)", data_len_range.end());
        }
        if let Some(capacity_range) = &filter.output_capacity_range {
            query_builder.and_where_ge("output.capacity", capacity_range.start());
            query_builder.and_where_lt("output.capacity", capacity_range.end());
        }
        if let Some(block_range) = &filter.block_range {
            query_builder.and_where_ge("block.block_number", block_range.start());
            query_builder.and_where_lt("block.block_number", block_range.end());
        }
        if filter.output_data.is_some() {
            match filter.output_data_filter_mode {
                Some(IndexerSearchMode::Prefix) | None => {
                    query_builder.and_where_ge("output.data", format!("${}", param_index));
                    *param_index += 1;
                    query_builder.and_where_lt("output.data", format!("${}", param_index));
                    *param_index += 1;
                }
                Some(IndexerSearchMode::Exact) => {
                    query_builder.and_where_eq("output.data", format!("${}", param_index));
                    *param_index += 1;
                }
                Some(IndexerSearchMode::Partial) => {
                    match db_driver {
                        DBDriver::Postgres => {
                            query_builder.and_where(format!("output.data LIKE ${}", param_index));
                        }
                        DBDriver::Sqlite => {
                            query_builder
                                .and_where(format!("instr(output.data, ${}) > 0", param_index));
                        }
                    }
                    *param_index += 1;
                }
            }
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L145-150)
```rust
        build_cell_filter(
            self.store.db_driver,
            &mut query_builder,
            &search_key,
            &mut param_index,
        );
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L189-220)
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
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L221-225)
```rust
        if !dead_cells.is_empty() {
            for (tx_hash, _) in dead_cells {
                query = query.bind(tx_hash)
            }
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L88-178)
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

        // sql string
        let sql = query_builder
            .sql()
            .map_err(|err| Error::DB(err.to_string()))?
            .trim_end_matches(';')
            .to_string();

        // bind
        let mut query = SQLXPool::new_query(&sql);
        query = query
            .bind(search_key.script.code_hash.as_bytes())
            .bind(search_key.script.hash_type as i16);
        match &search_key.script_search_mode {
            Some(IndexerSearchMode::Prefix) | None => {
                query = query
                    .bind(search_key.script.args.as_bytes())
                    .bind(get_binary_upper_boundary(search_key.script.args.as_bytes()));
            }
            Some(IndexerSearchMode::Exact) => {
                query = query.bind(search_key.script.args.as_bytes());
            }
            Some(IndexerSearchMode::Partial) => match self.store.db_driver {
                DBDriver::Postgres => {
                    let new_args = escape_and_wrap_for_postgres_like(&search_key.script.args);
                    query = query.bind(new_args);
                }
                DBDriver::Sqlite => {
                    query = query.bind(search_key.script.args.as_bytes());
                }
            },
        }
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
