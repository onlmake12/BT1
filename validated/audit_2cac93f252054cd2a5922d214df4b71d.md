### Title
Unbounded `dead_cells` NOT IN SQL Clause Causes RPC DoS via Tx Pool Flooding — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`, `util/indexer-sync/src/pool.rs`)

---

### Summary

When `index_tx_pool=true`, `Pool::new_transaction` inserts every input of every pending transaction into an unbounded `HashSet<OutPoint>`. Both `get_cells` and `get_cells_capacity` then iterate the entire set and emit a single SQL `NOT IN (($1,v1),($2,v2),…)` clause with one bound parameter per dead cell. SQLite rejects queries with more than 999 bound variables (its default `SQLITE_LIMIT_VARIABLE_NUMBER`); PostgreSQL rejects queries with more than 65 535 parameters. An attacker who submits enough valid pending transactions can push the parameter count past either threshold, causing every subsequent `get_cells` / `get_cells_capacity` RPC call to return a DB error for as long as those transactions remain pending.

---

### Finding Description

**`Pool::new_transaction` — no size guard** [1](#0-0) 

Every input of every accepted pending transaction is inserted into `dead_cells` with no cap.

**`get_cells` — unbounded NOT IN clause** [2](#0-1) 

All entries in `dead_cells` are collected into a `Vec`, then a single `NOT IN (…)` clause is appended to the SQL string with one `$N` placeholder per entry. There is no limit on how many placeholders are emitted.

**`get_cells_capacity` — same pattern** [3](#0-2) 

Identical unbounded NOT IN construction.

The `request_limit` field only caps the number of rows returned; it does not limit the NOT IN clause size. [4](#0-3) 

---

### Impact Explanation

- **SQLite backend**: `SQLITE_LIMIT_VARIABLE_NUMBER` defaults to 999. Once `dead_cells` exceeds 999 entries, every `get_cells` / `get_cells_capacity` call returns a DB error. The indexer is effectively unavailable for live-cell queries.
- **PostgreSQL backend**: The hard limit is 65 535 parameters. Reachable with a larger but still realistic number of pending inputs.
- The failure persists until the offending transactions are committed or evicted from the pool (hours, given the default `expiry_hours`).

---

### Likelihood Explanation

The tx pool enforces `max_tx_pool_size` (bytes) and `max_tx_verify_cycles`, but neither directly caps the total number of inputs across all pending transactions. [5](#0-4) 

An attacker needs only to own ≥1 000 live UTXOs (trivially achievable by first splitting funds with cheap transactions) and submit ~100 pending transactions each spending ~10 inputs. That is well within the default pool size limit and requires no privileged access — only standard RPC `send_transaction` calls. The economic cost is low because the attacker's funds are not lost; they remain in pending transactions.

---

### Recommendation

1. **Cap the NOT IN clause**: Before building the clause, truncate `dead_cells` to a safe maximum (e.g., 500 for SQLite, 30 000 for PostgreSQL) or chunk the query into multiple batches.
2. **Use a temporary table / CTE**: Insert dead cells into a temporary table and JOIN against it instead of using a variable-length NOT IN list.
3. **Enforce a hard limit on `Pool.dead_cells`**: Reject or evict entries once the set exceeds a configurable threshold, and expose that threshold as a node operator setting.

---

### Proof of Concept

1. Enable `index_tx_pool = true` in the indexer config.
2. Pre-fund a wallet with ≥1 000 live cells.
3. Submit 100 valid pending transactions, each spending 10 distinct inputs → `Pool.dead_cells` grows to 1 000 entries.
4. Call `get_cells` with any valid search key.
5. **Expected result (SQLite)**: the node returns a DB error such as `"too many SQL variables"` because the generated NOT IN clause contains 1 000 `$N` placeholders, exceeding SQLite's 999-variable limit.
6. The error persists for every `get_cells` / `get_cells_capacity` call until the pending transactions are committed or expire.

### Citations

**File:** util/indexer-sync/src/pool.rs (L40-44)
```rust
    pub fn new_transaction(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.insert(input.previous_output());
        }
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

**File:** util/rich-indexer/src/service.rs (L51-51)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
```

**File:** util/app-config/src/configs/tx_pool.rs (L11-27)
```rust
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
    /// txs need to pay larger fee rate than this for RBF
    #[serde(with = "FeeRateDef")]
    pub min_rbf_rate: FeeRate,
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
    #[serde(default = "default_max_tx_verify_workers")]
    pub max_tx_verify_workers: usize,
    /// max ancestors size limit for a single tx
    pub max_ancestors_count: usize,
    /// rejected tx time to live by days
```
