Audit Report

## Title
Unbounded `NOT IN` SQL Clause from tx-pool Dead Cells Degrades `get_cells` / `get_cells_capacity` — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`, `get_cells_capacity.rs`)

## Summary
When `index_tx_pool = true`, the `Pool` overlay accumulates every pending transaction's inputs as dead cells in an unbounded `HashSet<OutPoint>`. On every `get_cells` or `get_cells_capacity` call, the entire set is materialized inline into a SQL `NOT IN (($1, idx), ($2, idx), …)` clause with no size cap, no truncation, and no alternative join strategy. An unprivileged caller who fills the tx-pool with transactions spending distinct live cells causes the generated SQL to carry tens-of-thousands of tuple entries, degrading query planning and execution for every concurrent indexer query.

## Finding Description

The `Pool` struct holds an unbounded `HashSet<OutPoint>` with no capacity limit: [1](#0-0) 

`dead_cells()` returns an unbounded iterator over the full set: [2](#0-1) 

In `get_cells`, the entire set is collected and injected into the WHERE clause with one SQL parameter placeholder per entry: [3](#0-2) 

The identical pattern appears in `get_cells_capacity`: [4](#0-3) 

The `request_limit` field on `AsyncRichIndexerHandle` only caps the number of rows returned, not the size of the dead-cells filter: [5](#0-4) 

No guard, cap, or alternative join strategy exists anywhere in the call path. The tx-pool is bounded only by `max_tx_pool_size = 180_000_000` (180 MB), which at a minimum CKB transaction size of ~100–200 bytes allows on the order of 10,000–100,000+ pending transactions, each contributing one tuple to the `NOT IN` list.

## Impact Explanation

This is a **Low (501–2000 points)** finding: an important performance degradation for CKB's indexer RPC. SQL planners (both SQLite and PostgreSQL) degrade super-linearly with large `NOT IN` lists — the planner must enumerate all values to build a hash or scan structure, and every candidate row is checked against the full list. With tens of thousands of entries the generated SQL string itself grows to several megabytes, compounding memory pressure on the DB process. The degradation affects every concurrent `get_cells` / `get_cells_capacity` caller, not only the attacker's requests. This does not crash the node process itself, does not affect consensus, and does not cause P2P network congestion, so it does not qualify for High or Critical.

## Likelihood Explanation

The attack path is fully unprivileged. The only precondition is `index_tx_pool = true`, which is the documented purpose of the rich-indexer pool overlay. The attacker submits transactions via the standard `send_transaction` RPC, each spending a distinct live cell. No PoW, no privileged access, and no victim mistakes are required. Organic high-throughput activity produces the same effect without any adversarial intent.

## Recommendation

Replace the inline `NOT IN (…)` with a temporary table or CTE populated via bulk insert, then use a `NOT EXISTS` / anti-join against it so the DB can execute it with an index scan. Alternatively, cap the number of dead cells materialized per query (e.g., skip pool filtering above a configurable threshold and document the accuracy trade-off). A third option is to batch the dead-cell check into chunks and intersect results in application code.

## Proof of Concept

1. Configure a CKB node with `index_tx_pool = true` and the `RichIndexer` RPC module enabled.
2. Pre-fund N addresses with distinct live cells (N = 10,000 is sufficient to observe degradation).
3. Submit N transactions via `send_transaction`, each spending one distinct live cell.
4. Call `get_cells` with any valid `search_key`.
5. Instrument `SQLXPool::fetch_all` to log the generated SQL — the `NOT IN` clause will contain N tuple entries.
6. Benchmark `get_cells` latency as a function of N; observe super-linear growth in both query-planning time and execution time.

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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L23-27)
```rust
pub struct AsyncRichIndexerHandle {
    store: SQLXPool,
    pool: Option<Arc<RwLock<Pool>>>,
    request_limit: usize,
}
```
