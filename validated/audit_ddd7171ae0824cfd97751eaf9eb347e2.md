Let me check the pool size limits and the SQLite/PostgreSQL variable limits more carefully.

### Title
Unbounded `dead_cells` NOT IN Clause Causes Permanent DoS on Rich Indexer RPC — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`, `get_cells_capacity.rs`)

---

### Summary

When `index_tx_pool = true` is configured for the rich indexer, every pending transaction's inputs are tracked in an unbounded `HashSet<OutPoint>` (`Pool.dead_cells`). On every `get_cells` or `get_cells_capacity` RPC call, **all** dead cells are collected without any cap and embedded as a `NOT IN (($1, idx1), ($2, idx2), ...)` clause in the SQL query. An unprivileged attacker who floods the tx pool causes this clause to grow to tens of thousands of entries, exceeding SQLite's hard limit of 999 bind variables (or PostgreSQL's 65535), making every subsequent `get_cells`/`get_cells_capacity` call return a DB error for the lifetime of the pool state.

---

### Finding Description

**`Pool.dead_cells` is unbounded.** [1](#0-0) 

The `HashSet<OutPoint>` grows with every new pool transaction and has no size cap independent of the tx pool's byte-size limit.

**`get_cells` collects ALL dead cells with no limit.** [2](#0-1) 

Every entry in `dead_cells` becomes one `($N, output_index)` placeholder in the SQL `NOT IN (...)` clause. The same pattern is present in `get_cells_capacity`: [3](#0-2) 

**The tx pool is bounded by bytes, not transaction count.** [4](#0-3) 

The default `max_tx_pool_size = 180_000_000` (180 MB). A minimum-size CKB transaction is ~100–200 bytes serialized, allowing ~900K–1.8M transactions in the pool simultaneously. Each transaction contributes at least one dead cell. There is no per-transaction-count cap.

**`request_limit` only bounds the RPC `limit` parameter, not dead cells.** [5](#0-4) 

The `request_limit` check only validates the caller-supplied `limit` (result page size). It does not bound the number of dead cells embedded in the query.

**SQLite's bind variable limit is 999 by default.** PostgreSQL's is 65535. With a pool containing more than 999 transactions (trivially achievable), every `get_cells` and `get_cells_capacity` call will fail with a DB error (`SQLITE_RANGE` / `too many bind variables`), permanently breaking the rich indexer RPC until the pool drains.

Additionally, building a SQL string with millions of placeholders allocates significant memory and CPU on every RPC call, causing severe performance degradation even before the DB limit is hit.

---

### Impact Explanation

- `get_cells` and `get_cells_capacity` become permanently non-functional (return DB errors on every call) once the pool exceeds ~999 pending transactions (SQLite) or ~65535 (PostgreSQL).
- Each RPC call while the pool is large allocates O(N) memory to build the placeholder string, causing CPU/memory pressure proportional to pool size.
- The CKB node itself continues to operate; only the rich indexer RPC is affected.

---

### Likelihood Explanation

- Requires non-default configuration: `index_tx_pool = true` and the `RichIndexer` RPC module enabled. [6](#0-5) 
- Both are documented, supported production options. Operators running the rich indexer for wallet/dApp infrastructure commonly enable `index_tx_pool` for accurate live-cell results.
- Flooding the tx pool with valid low-fee transactions is a standard, unprivileged operation available via P2P relay or the `send_transaction` RPC.
- The 999-entry SQLite threshold is crossed with fewer than 1000 pending transactions — a trivially small pool for a busy network.

---

### Recommendation

Cap the number of dead cells used in the NOT IN clause. Options:

1. **Hard cap**: Truncate `dead_cells` to a safe maximum (e.g., 500 for SQLite, 32000 for PostgreSQL) before building the clause. Document that results may include a small number of pool-spent cells when the pool is very large.
2. **Chunked queries**: Split the NOT IN list into batches of ≤ 999 entries and intersect results in application code.
3. **Temporary table / CTE**: Insert dead cells into a temporary table and use `NOT IN (SELECT ...)` to avoid bind variable limits entirely.
4. **Separate pool-spent filter**: After fetching results from the DB, filter out pool-spent cells in Rust rather than in SQL.

---

### Proof of Concept

```
Preconditions:
  - CKB node with [indexer] index_tx_pool = true, RichIndexer RPC module enabled, SQLite backend

Step 1: Submit 1000+ valid pending transactions (each spending one distinct live cell)
        via send_transaction RPC or P2P relay.

Step 2: Call get_cells with any search_key.

Expected (vulnerable): DB error — "too many SQL variables" / SQLITE_RANGE
  because the NOT IN clause contains 1000+ bind parameters, exceeding SQLite's limit of 999.

Step 3: Observe that every subsequent get_cells / get_cells_capacity call fails
        until the pool drains (transactions committed or evicted).
```

The root cause is at:
- [7](#0-6) 
- [8](#0-7)

### Citations

**File:** util/indexer-sync/src/pool.rs (L20-22)
```rust
pub struct Pool {
    dead_cells: HashSet<OutPoint>,
}
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L25-34)
```rust
        let limit = limit.value();
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L110-135)
```rust
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
            query_builder.and_where(format!("(tx_hash, output_index) NOT IN ({})", placeholders));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L74-102)
```rust
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
```

**File:** resource/ckb.toml (L211-211)
```text
max_tx_pool_size = 180_000_000 # 180mb
```

**File:** util/app-config/src/configs/indexer.rs (L57-57)
```rust
            index_tx_pool: false,
```
