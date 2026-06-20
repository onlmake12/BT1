The code confirms the asymmetry. Here is the full analysis:

---

### Title
Missing `request_limit` enforcement in `get_cells_capacity` enables unbounded DB aggregate scan — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

### Summary
`get_cells` and `get_transactions` both enforce `self.request_limit` before executing any query. `get_cells_capacity` accepts the same `IndexerSearchKey` (including broad prefix/partial search modes) but issues an unbounded `SELECT SUM(output.capacity)` aggregate with no row-count cap, no `LIMIT` clause, and no check against `self.request_limit`. A single unauthenticated RPC call can force the DB to scan every matching row in the `output` table.

### Finding Description
`get_cells` enforces the limit at lines 29–33:

```rust
if limit as usize > self.request_limit {
    return Err(Error::invalid_params(format!(
        "limit must be less than {}",
        self.request_limit,
    )));
}
``` [1](#0-0) 

`get_transactions` has the identical guard: [2](#0-1) 

`get_cells_capacity` has **no such guard**. Its signature takes only `search_key: IndexerSearchKey` — no `limit` parameter — and the generated SQL is a bare aggregate:

```rust
query_builder.field("CAST(SUM(output.capacity) AS BIGINT) AS total_capacity");
``` [3](#0-2) 

No `LIMIT` clause is ever appended to `query_builder`, and `self.request_limit` is never read inside this function. [4](#0-3) 

The `request_limit` field exists on `AsyncRichIndexerHandle` and is populated at construction time: [5](#0-4) 

The RPC layer passes the call straight through with no additional guard: [6](#0-5) 

### Impact Explanation
With `script_search_mode = Prefix` and an empty `args` field, the prefix range covers every script row sharing a given `code_hash`. On a mainnet-scale node (tens of millions of live cells), a single `get_cells_capacity` call forces the DB engine to read and aggregate every matching `output` row. Because the call returns a single scalar, the caller pays essentially nothing (one HTTP request, one small JSON response) while the node's DB thread is pinned for the duration of the full-table aggregate. Repeated calls from one or more clients can saturate DB I/O and CPU, degrading or blocking all other indexer RPC service.

### Likelihood Explanation
The RPC endpoint is exposed to any client that can reach the node's RPC port (default: localhost, but commonly exposed for public indexer services). No authentication, PoW, or stake is required. The attack is trivially scriptable: a loop sending `get_cells_capacity` with a broad prefix is sufficient. The asymmetric enforcement (blocked by `get_cells`, silently allowed by `get_cells_capacity`) means the operator's configured `request_limit` provides a false sense of protection.

### Recommendation
Add a row-count guard inside `get_cells_capacity`. The most direct fix mirrors the pattern already used in `get_cells` and `get_transactions`: before executing the aggregate query, run a `SELECT COUNT(*) … LIMIT (request_limit + 1)` sub-query (or add a `LIMIT` to the aggregate's inner scan) and reject the request if the count exceeds `self.request_limit`. Alternatively, wrap the aggregate in a subquery that is itself `LIMIT`-ed to `self.request_limit` rows before the `SUM` is computed.

### Proof of Concept
1. Start a CKB node with `rich-indexer` enabled and `request_limit = 1` in `ckb.toml`.
2. Call `get_cells` with `limit = 2` and any broad `search_key` → expect `Error: limit must be less than 1`.
3. Call `get_cells_capacity` with the **identical** `search_key` (no `limit` parameter needed) → expect a successful response after a full DB aggregate scan.
4. The asymmetric result proves `request_limit` is not enforced for `get_cells_capacity`.

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L29-33)
```rust
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L27-32)
```rust
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L11-226)
```rust
impl AsyncRichIndexerHandle {
    /// Get cells_capacity by specified search_key
    pub async fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>, Error> {
        // sub query for script
        let mut param_index = 1;
        let script_sub_query_sql = build_query_script_id_sql(
            self.store.db_driver,
            &search_key.script_search_mode,
            &mut param_index,
        )?;

        // query output
        let mut query_builder = SqlBuilder::select_from("output");
        query_builder.field("CAST(SUM(output.capacity) AS BIGINT) AS total_capacity");
        query_builder.join(format!("{} AS query_script", script_sub_query_sql));
        match search_key.script_type {
            IndexerScriptType::Lock => {
                query_builder.on("output.lock_script_id = query_script.id");
            }
            IndexerScriptType::Type => {
                query_builder.on("output.type_script_id = query_script.id");
            }
        }
        let mut joined_ckb_transaction = false;
        if let Some(ref filter) = search_key.filter
            && filter.block_range.is_some()
        {
            query_builder
                .join("ckb_transaction")
                .on("output.tx_id = ckb_transaction.id")
                .join("block")
                .on("ckb_transaction.block_id = block.id");
            joined_ckb_transaction = true;
        }
        if self.pool.is_some() && !joined_ckb_transaction {
            query_builder
                .join("ckb_transaction")
                .on("output.tx_id = ckb_transaction.id");
        }
        if let Some(ref filter) = search_key.filter
            && (filter.script.is_some() || filter.script_len_range.is_some())
        {
            match search_key.script_type {
                IndexerScriptType::Lock => {
                    query_builder
                        .left()
                        .join(name!("script";"type_script"))
                        .on("output.type_script_id = type_script.id");
                }
                IndexerScriptType::Type => {
                    query_builder
                        .left()
                        .join(name!("script";"lock_script"))
                        .on("output.lock_script_id = lock_script.id");
                }
            }
        }
        query_builder.and_where("output.is_spent = 0"); // live cells

        // filter cells in pool
        let mut dead_cells = Vec::new();
        if let Some(pool) = self
            .pool
            .as_ref()
            .map(|pool| pool.read().expect("acquire lock"))
        {
            dead_cells = pool
                .dead_cells()
                .map(|out_point| {
                    let tx_hash: H256 = out_point.tx_hash().into();
                    (tx_hash.as_bytes().to_vec(), out_point.index().into())
                })
                .collect::<Vec<(_, u32)>>()
        }
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

        let mut tx = self
            .store
            .transaction()
            .await
            .map_err(|err| Error::DB(err.to_string()))?;

        // fetch
        let capacity = query
            .fetch_optional(&mut *tx)
            .await
            .map_err(|err| Error::DB(err.to_string()))?
            .and_then(|row| row.try_get::<i64, _>("total_capacity").ok());
        let capacity = match capacity {
            Some(capacity) => capacity as u64,
            None => return Ok(None),
        };

        let (block_hash, block_number) = SQLXPool::new_query(
            r#"
                SELECT block_hash, block_number FROM block
                ORDER BY id DESC
                LIMIT 1
                "#,
        )
        .fetch_optional(&mut *tx)
        .await
        .map(|res| {
            res.map(|row| {
                (
                    bytes_to_h256(row.get("block_hash")),
                    row.get::<i64, _>("block_number") as u64,
                )
            })
        })
        .map_err(|err| Error::DB(err.to_string()))?
        .unwrap();

        tx.commit()
            .await
            .map_err(|err| Error::DB(err.to_string()))?;

        Ok(Some(IndexerCellsCapacity {
            capacity: capacity.into(),
            block_hash,
            block_number: block_number.into(),
        }))
    }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L23-37)
```rust
pub struct AsyncRichIndexerHandle {
    store: SQLXPool,
    pool: Option<Arc<RwLock<Pool>>>,
    request_limit: usize,
}

impl AsyncRichIndexerHandle {
    /// Construct new AsyncRichIndexerHandle instance
    pub fn new(store: SQLXPool, pool: Option<Arc<RwLock<Pool>>>, request_limit: usize) -> Self {
        Self {
            store,
            pool,
            request_limit,
        }
    }
```

**File:** rpc/src/module/rich_indexer.rs (L206-214)
```rust
    async fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>> {
        self.handle
            .get_cells_capacity(search_key)
            .await
            .map_err(|e| RPCError::custom(RPCError::Indexer, e))
    }
```
