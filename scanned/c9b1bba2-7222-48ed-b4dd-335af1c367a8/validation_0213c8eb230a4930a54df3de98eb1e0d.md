The code is concrete and I can trace the full path. Let me synthesize the findings.

---

**Code path confirmed:**

1. `Pool.dead_cells` is an unbounded `HashSet<OutPoint>` — no cap enforced anywhere. [1](#0-0) 

2. Every accepted mempool transaction inserts all its inputs into that set with no size guard. [2](#0-1) 

3. `get_cells` collects **all** dead cells and emits one SQL placeholder per cell into a `NOT IN (...)` clause — no bound, no chunking, no temp-table fallback. [3](#0-2) 

4. `get_cells_capacity` has the identical pattern. [4](#0-3) 

---

**Reachability and limits:**

- SQLite's default `SQLITE_MAX_VARIABLE_NUMBER` is **999**. Submitting just ~1,000 mempool transactions (each spending one UTXO) pushes `dead_cells` past that limit, causing every subsequent `get_cells` / `get_cells_capacity` call to return a DB error.
- PostgreSQL's hard limit is **65,535** bind parameters. The tx-pool's default `max_tx_pool_size` of 180 MB can hold far more than 65,535 single-input transactions (minimum ~100 bytes each → ~1.8 M transactions), so the PostgreSQL limit is also reachable.
- The attacker is unprivileged: submitting transactions to the mempool is a standard, open RPC/P2P operation.

---

**Impact correction vs. the question:**

The node process itself does **not** crash. The error is caught and returned as `Error::DB(...)` to the RPC caller. However, the entire rich-indexer query surface (`get_cells`, `get_cells_capacity`) becomes permanently non-functional for every caller as long as the mempool stays large — a concrete, sustained DoS of the indexer RPC layer.

---

### Title
Unbounded `NOT IN` SQL clause from `pool.dead_cells()` causes persistent DoS of rich-indexer RPC — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`, `get_cells_capacity.rs`)

### Summary
When the rich-indexer pool integration is enabled (`pool: Some(...)`), `get_cells` and `get_cells_capacity` collect every dead cell from the mempool and embed one SQL bind parameter per cell into a `NOT IN (…)` clause. There is no upper bound on this list. An unprivileged attacker who floods the mempool with transactions that each consume distinct UTXOs can push the parameter count past SQLite's 999-parameter limit (or PostgreSQL's 65,535-parameter limit), causing every subsequent indexer RPC call to fail with a DB error for as long as the mempool remains large.

### Finding Description
In `get_cells.rs` lines 124–135 and `get_cells_capacity.rs` lines 88–102, the code iterates over `pool.dead_cells()` — which returns the entire `HashSet<OutPoint>` stored in `Pool` — and constructs a SQL string of the form:

```sql
(tx_hash, output_index) NOT IN (($1, 0), ($2, 1), ..., ($N, N-1))
```

`N` is unbounded: `Pool.dead_cells` is a plain `HashSet` with no capacity cap, and `Pool::new_transaction` inserts every input of every accepted mempool transaction without any guard. [5](#0-4) 

SQLite enforces `SQLITE_MAX_VARIABLE_NUMBER` (default 999) at query execution time; exceeding it returns an error. PostgreSQL enforces a 65,535-parameter limit at the protocol level. Both limits are reachable through normal mempool flooding.

### Impact Explanation
All callers of `get_cells` and `get_cells_capacity` RPC methods receive persistent `Error::DB` responses for the duration of the attack. Wallets, dApps, and tooling that depend on the rich-indexer for live-cell queries are completely blinded. The node's consensus and block-production paths are unaffected, but the indexer service is rendered non-functional.

### Likelihood Explanation
- Submitting transactions to the mempool is an open, unprivileged operation available over RPC and P2P.
- SQLite's 999-parameter limit is crossed with as few as ~1,000 single-input mempool transactions — trivially achievable.
- The tx-pool's 180 MB default size cap does not prevent this: 1,000 minimal transactions occupy well under 1 MB.
- No PoW, no privileged access, no special configuration is required beyond `index_tx_pool: true`.

### Recommendation
Replace the inline `NOT IN (…)` expansion with a bounded approach:

1. **Temp table / CTE**: Insert dead cells into a temporary table or CTE and join against it. Both SQLite and PostgreSQL support this without parameter-count limits.
2. **Chunked exclusion**: Split dead cells into chunks of, e.g., 500 and apply multiple `NOT IN` clauses, or better, use the temp-table approach.
3. **Hard cap with error**: If the dead-cell count exceeds a configurable threshold (e.g., 500), return a clear `Error::Params` rather than attempting an oversized query.

### Proof of Concept
```rust
// Populate a mock Pool with 1,000 dead cells (SQLite limit = 999)
let mut pool = Pool::default();
for i in 0u32..1_000 {
    let tx = TransactionBuilder::default()
        .input(CellInput::new(OutPoint::new(h256!("0xdead").pack(), i), 0))
        .build();
    pool.new_transaction(&tx.into_view());
}
// pool.dead_cells().count() == 1_000
// get_cells() will now build a NOT IN clause with 1,000 bind parameters,
// exceeding SQLite's SQLITE_MAX_VARIABLE_NUMBER (999) and returning Error::DB.
// On PostgreSQL the same pattern holds at 65,535 dead cells.
``` [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** util/indexer-sync/src/pool.rs (L19-44)
```rust
#[derive(Default)]
pub struct Pool {
    dead_cells: HashSet<OutPoint>,
}

impl Pool {
    /// the tx has been committed in a block, it should be removed from pending dead cells
    pub fn transaction_committed(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.remove(&input.previous_output());
        }
    }

    /// the tx has been rejected for some reason, it should be removed from pending dead cells
    pub fn transaction_rejected(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.remove(&input.previous_output());
        }
    }

    /// a new tx is submitted to the pool, mark its inputs as dead cells
    pub fn new_transaction(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.insert(input.previous_output());
        }
    }
```

**File:** util/indexer-sync/src/pool.rs (L59-61)
```rust
    pub fn dead_cells(&self) -> impl Iterator<Item = &OutPoint> {
        self.dead_cells.iter()
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L73-102)
```rust
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
```
