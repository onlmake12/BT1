The code confirms all claims. Let me verify the tx-pool size config and check for any guards I may have missed.

Audit Report

## Title
Unbounded `NOT IN` SQL Clause from tx-pool Dead Cells Degrades `get_cells` / `get_cells_capacity` — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`, `get_cells_capacity.rs`)

## Summary
When `index_tx_pool = true`, the `Pool` overlay accumulates every pending transaction's inputs as `dead_cells` with no size bound. On every `get_cells` or `get_cells_capacity` call, the entire set is materialized inline into a SQL `NOT IN (($1, idx), ($2, idx), …)` clause. An attacker who fills the tx-pool with transactions spending distinct live cells causes the generated SQL to carry thousands-to-hundreds-of-thousands of tuple entries, degrading query planning and execution for all concurrent indexer callers.

## Finding Description
`Pool` is an unbounded `HashSet<OutPoint>` with no capacity cap: [1](#0-0) 

`dead_cells()` returns an unbounded iterator over that set: [2](#0-1) 

In `get_cells`, the entire set is collected and injected into the WHERE clause with no truncation or cap: [3](#0-2) 

The identical pattern appears in `get_cells_capacity`: [4](#0-3) 

No guard, cap, or alternative join strategy exists anywhere in the call path. The tx-pool is bounded only by `max_tx_pool_size = 180_000_000` (180 MB): [5](#0-4) 

With minimum CKB transaction serialized size of ~100–200 bytes, the pool can hold on the order of 10,000–100,000+ transactions. Each transaction spending a distinct live cell contributes one tuple to the `NOT IN` list, causing super-linear growth in query planning and execution cost for every `get_cells` / `get_cells_capacity` call.

## Impact Explanation
This matches **Low (501–2000 points): Any other important performance improvements for CKB**. The degradation is confined to the rich-indexer RPC layer (`get_cells`, `get_cells_capacity`); it does not crash the core CKB node or cause consensus deviation. However, at pool saturation the indexer RPC becomes effectively unusable for all callers, not just the attacker, making this a meaningful denial-of-service against the indexer subsystem.

## Likelihood Explanation
The attack requires `index_tx_pool = true` (the documented purpose of the pool overlay) and enough live cells to submit many transactions via the standard `send_transaction` RPC — no privileged access, no PoW, no social engineering. The attacker must hold sufficient CKB to fund the inputs, which is a non-trivial but realistic cost. Organic high-throughput activity can trigger the same degradation without any malicious intent.

## Recommendation
Replace the inline `NOT IN (…)` with a temporary table or CTE populated via bulk insert, then use a `NOT EXISTS` / anti-join against it so the DB can use an index scan. Alternatively, cap the number of dead cells materialized per query (e.g., skip pool filtering above a configurable threshold and document the trade-off). Either approach eliminates the super-linear planning cost.

## Proof of Concept
1. Configure node with `index_tx_pool = true` and the `RichIndexer` RPC module enabled.
2. Pre-fund N addresses with distinct live cells (N ≥ 10,000 is sufficient to observe degradation).
3. Submit N transactions via `send_transaction`, each spending one distinct live cell.
4. Call `get_cells` with any `search_key`.
5. Log the generated SQL from `SQLXPool::fetch_all` — the `NOT IN` clause will contain N tuples.
6. Benchmark `get_cells` latency as a function of N; observe super-linear growth due to query-plan cost.

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
