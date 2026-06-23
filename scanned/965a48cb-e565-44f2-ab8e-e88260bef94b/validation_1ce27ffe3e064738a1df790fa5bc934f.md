The code is clear. Let me verify the tx-pool size bounds to understand the maximum scale of the attack.

The code confirms the vulnerability. There is no cap anywhere on `dead_cells` materialized into the SQL clause, and no guard in the `Pool` struct either.

---

### Title
Unbounded `NOT IN` SQL Clause from tx-pool Dead Cells Degrades `get_cells` / `get_cells_capacity` — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`, `get_cells_capacity.rs`)

### Summary
When `index_tx_pool = true`, every pending transaction's inputs are tracked as `dead_cells` in the indexer's `Pool` overlay. On every `get_cells` or `get_cells_capacity` call, **all** dead cells are materialized inline into a SQL `NOT IN (($1, idx), ($2, idx), …)` clause with no size cap. An unprivileged caller who fills the tx-pool with transactions spending distinct live cells causes the generated SQL to carry thousands-to-hundreds-of-thousands of tuple entries, degrading both query planning and execution for every subsequent indexer query.

### Finding Description

`Pool::dead_cells()` returns an unbounded iterator over a `HashSet<OutPoint>`: [1](#0-0) 

In `get_cells`, the entire set is collected and injected into the WHERE clause: [2](#0-1) 

The identical pattern appears in `get_cells_capacity`: [3](#0-2) 

No cap, truncation, or alternative join strategy is applied at any point. The `Pool` struct itself has no size limit: [4](#0-3) 

### Impact Explanation

The tx-pool is bounded by `max_tx_pool_size` (default 180 MB): [5](#0-4) 

With a minimum CKB transaction serialized size of ~100–200 bytes, the pool can hold on the order of 10,000–100,000+ transactions. Each transaction spending a distinct live cell contributes one tuple to the `NOT IN` list. SQL planners (both SQLite and PostgreSQL) degrade significantly with large `NOT IN` lists:

- **Query planning**: the planner must enumerate all values to build a hash or scan structure.
- **Query execution**: every candidate row is checked against the full list.
- **Compounding**: the degradation affects every `get_cells` / `get_cells_capacity` call concurrently, not just the attacker's.

### Likelihood Explanation

The attack path is fully unprivileged and reachable via the standard `send_transaction` RPC. The attacker needs only to own enough live cells to submit many transactions (or organic high-throughput activity achieves the same effect). No PoW, no privileged access, no social engineering is required. The `index_tx_pool = true` configuration is the only precondition, and it is the documented purpose of the rich-indexer pool overlay.

### Recommendation

Replace the inline `NOT IN (…)` with a temporary table or CTE populated via bulk insert, then use a `NOT EXISTS` / anti-join against it. Alternatively, cap the number of dead cells materialized per query (e.g., skip pool filtering above a threshold and document the trade-off), or use a keyset-based anti-join that the DB can execute with an index scan.

### Proof of Concept

1. Configure node with `index_tx_pool = true` and the `RichIndexer` RPC module enabled.
2. Pre-fund N addresses with live cells (N = 10,000 is sufficient to observe degradation).
3. Submit N transactions via `send_transaction`, each spending one distinct live cell.
4. Call `get_cells` with any search key.
5. Instrument `SQLXPool::fetch_all` to log the generated SQL — the `NOT IN` clause will contain N tuples.
6. Benchmark `get_cells` latency as a function of N; observe super-linear growth due to query-plan cost. [6](#0-5) [7](#0-6)

### Citations

**File:** util/indexer-sync/src/pool.rs (L19-22)
```rust
#[derive(Default)]
pub struct Pool {
    dead_cells: HashSet<OutPoint>,
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
