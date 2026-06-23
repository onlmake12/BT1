The code is fully readable. Let me trace the exact parameter numbering through both the SQL construction path and the binding path.

**SQL construction order (param_index tracking):**

`build_query_script_sql` (Prefix/None mode) consumes `$1`–`$4` (code_hash, hash_type, args_lower, args_upper), leaving `param_index = 5`. [1](#0-0) 

Dead-cell placeholders are then built using the current `param_index` (starting at 5), so N dead cells get `$5`…`$5+N-1`, and `param_index` advances to `5+N`. [2](#0-1) 

`build_cell_filter` is called **after** the dead-cell block, so filter params (e.g., `output.data = $6` for N=1) receive placeholder numbers `$5+N` and above. [3](#0-2) [4](#0-3) 

**Binding order:**

After the script params (positions 1–4), the binding code binds **filter params first** (lines 189–220), then **dead-cell tx_hashes** (lines 221–225). [5](#0-4) 

**The mismatch is real and concrete.** For N=1 dead cell + `filter.output_data` in Exact mode (Prefix script search):

| Placeholder in SQL | Expected value | Actually bound value |
|---|---|---|
| `$5` | dead-cell tx_hash (32 bytes) | `output_data` bytes |
| `$6` | `output_data` bytes | dead-cell tx_hash (32 bytes) |

The `NOT IN ($5, output_index)` clause receives `output_data` bytes where it expects a tx_hash, so it never matches any real tx_hash → dead/spent cells are **not excluded**. The `output.data = $6` clause receives a tx_hash where it expects output data → live cells are **incorrectly filtered out**.

---

### Title
SQL Parameter Binding Order Mismatch in `get_cells` Causes Dead Cells to Bypass Exclusion and Filter Params to Be Misapplied — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`)

### Summary
When a `get_cells` RPC call includes both a `filter` (e.g., `filter.output_data`) and the node's tx-pool contains at least one dead cell, the SQL placeholder numbering assigns lower indices to dead-cell `NOT IN` params and higher indices to filter params, but the `.bind()` call sequence does the opposite: filter values are bound first, dead-cell tx_hashes last. The wrong bytes are substituted into each placeholder.

### Finding Description
In `get_cells.rs`, `param_index` is a shared counter that drives both SQL placeholder generation and the binding sequence. The SQL is built in this order:

1. `build_query_script_sql` → `$1`–`$4`
2. Dead-cell `NOT IN` placeholders → `$5`…`$5+N-1`
3. `build_cell_filter` → `$5+N`…

But `.bind()` calls proceed:

1. Script params → positions 1–4 ✓
2. Filter params (lines 189–220) → positions 5… ✗ (SQL expects dead-cell tx_hashes here)
3. Dead-cell tx_hashes (lines 221–225) → positions 5+M… ✗ (SQL expects filter data here) [2](#0-1) [3](#0-2) [5](#0-4) 

### Impact Explanation
- **Spent cells appear live:** The `NOT IN` clause receives `output_data` bytes (not a tx_hash), so it never matches any real outpoint. Cells consumed by pending pool transactions are returned as live UTXOs.
- **Live cells incorrectly excluded:** The `output.data = $N` filter receives a tx_hash (32 raw bytes), so no cell's data matches, and the entire result set for that filter is empty or wrong.

Wallets and dApps relying on the indexer will see an incorrect UTXO set: they may attempt to spend already-consumed cells (producing invalid transactions) or fail to find spendable cells entirely.

### Likelihood Explanation
The trigger condition is common in production: any node with pending transactions (dead cells in the pool) combined with any `get_cells` call that uses `filter.output_data` or `filter.script`. No special privileges are required — this is a standard public RPC endpoint.

### Recommendation
Reorder the `.bind()` calls to match the SQL placeholder order: bind dead-cell tx_hashes **before** filter params, immediately after the script params. Alternatively, restructure the SQL construction so that `build_cell_filter` is called before the dead-cell block, keeping both orderings consistent.

### Proof of Concept
With Prefix script search mode, N=1 dead cell (tx_hash = `[0xAA; 32]`), and `filter.output_data = Exact([0xBB; 4])`:

- SQL generated: `... NOT IN (($5, <idx>)) ... AND output.data = $6`
- Binding sequence: `$5` ← `[0xBB; 4]` (output_data), `$6` ← `[0xAA; 32]` (tx_hash)
- Result: `NOT IN` never fires (wrong bytes), `output.data = [0xAA;32]` matches nothing → dead cell leaks into results, live cells with `output_data=[0xBB;4]` are dropped.

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L89-98)
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L222-247)
```rust
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L189-225)
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
