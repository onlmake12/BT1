The code exactly matches all citations in the report. All four code references are confirmed:

- `Pool.dead_cells` is an unbounded `HashSet<OutPoint>` with no cap [1](#0-0) 
- `dead_cells()` returns an uncapped iterator [2](#0-1) 
- `get_cells` materializes the full set into a `NOT IN (…)` clause with no guard [3](#0-2) 
- `get_cells_capacity` has the identical pattern [4](#0-3) 

---

Audit Report

## Title
Unbounded `NOT IN` SQL Clause from tx-pool Dead Cells Degrades `get_cells` / `get_cells_capacity` — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`, `get_cells_capacity.rs`)

## Summary
When `index_tx_pool = true`, the `Pool` overlay accumulates every pending transaction's inputs as `dead_cells` in an unbounded `HashSet`. On every `get_cells` or `get_cells_capacity` call, the entire set is materialized inline into a SQL `NOT IN (($1, idx), ($2, idx), …)` clause with no size cap, no truncation, and no alternative join strategy. An attacker or organic high-throughput activity that fills the tx-pool with transactions spending distinct live cells causes the generated SQL to carry tens-of-thousands of tuple entries, degrading query planning and execution for every concurrent indexer query.

## Finding Description
`Pool` in `util/indexer-sync/src/pool.rs` holds an unbounded `HashSet<OutPoint>` (lines 19–22). `dead_cells()` returns an uncapped iterator over it (lines 59–61). In `get_cells`, the entire set is collected and injected into the WHERE clause as a raw `NOT IN (…)` string with one placeholder per entry (lines 110–135). The identical pattern appears in `get_cells_capacity` (lines 74–102). No guard, cap, or alternative join strategy exists anywhere in either path. The tx-pool is bounded only by byte size (`max_tx_pool_size = 180_000_000`), not by transaction count, allowing tens-of-thousands of distinct dead-cell entries to accumulate. Entries are only removed on `transaction_committed` or `transaction_rejected` events, so a sustained stream of pending transactions keeps the set large indefinitely.

## Impact Explanation
This matches **Low (501–2000 points) — Any other important performance improvements for CKB**. The impact is super-linear degradation of the `get_cells` and `get_cells_capacity` RPC endpoints: SQL planners (both SQLite and PostgreSQL) must enumerate all `NOT IN` values at plan time and check every candidate row against the full list at execution time. With a 180 MB pool and minimum transaction sizes of ~100–200 bytes, the pool can hold on the order of 10,000–100,000+ transactions, each contributing one tuple to the clause. This does not crash the node or affect consensus, but it renders the indexer RPC progressively unusable under realistic high-throughput conditions, which is a concrete and important performance impact for node operators relying on the rich-indexer.

## Likelihood Explanation
The precondition is `index_tx_pool = true`, which is the documented purpose of the rich-indexer pool overlay. The trigger is reachable via the standard `send_transaction` RPC. An attacker needs enough live cells to submit many transactions; however, organic high-throughput network activity produces the same effect without any adversarial intent. No privileged access, PoW, or social engineering is required beyond holding live cells.

## Recommendation
Replace the inline `NOT IN (…)` with a temporary table or CTE populated via bulk insert, then use a `NOT EXISTS` / anti-join against it so the database can use an index scan. Alternatively, cap the number of dead cells materialized per query (e.g., skip pool filtering above a configurable threshold and document the trade-off). Both `get_cells.rs` and `get_cells_capacity.rs` must be updated consistently.

## Proof of Concept
1. Configure node with `index_tx_pool = true` and the `RichIndexer` RPC module enabled.
2. Pre-fund N addresses with distinct live cells (N ≥ 10,000 is sufficient to observe degradation).
3. Submit N transactions via `send_transaction`, each spending one distinct live cell.
4. Call `get_cells` with any search key.
5. Log the generated SQL at `SQLXPool::fetch_all` — the `NOT IN` clause will contain N tuples.
6. Benchmark `get_cells` latency as a function of N; observe super-linear growth confirming query-plan cost dominates.

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
