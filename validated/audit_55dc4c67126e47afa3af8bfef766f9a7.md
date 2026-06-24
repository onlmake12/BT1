Audit Report

## Title
Unbounded `NOT IN` SQL Clause in `get_cells` Scales O(N) with tx-pool Dead Cells — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`)

## Summary
When `index_tx_pool = true` is configured, `get_cells` and `get_cells_capacity` collect every entry from `Pool::dead_cells` and emit one SQL placeholder per entry into a `NOT IN (…)` clause with no cap. An attacker who fills the tx-pool with transactions each spending a distinct UTXO forces every subsequent `get_cells` call to construct and submit a proportionally large SQL query, degrading RPC performance linearly with pool size up to effective unresponsiveness.

## Finding Description
**Unbounded `Pool::dead_cells`.**
`util/indexer-sync/src/pool.rs` line 21 defines `dead_cells: HashSet<OutPoint>` with no size cap. [1](#0-0)  `new_transaction` at lines 40–44 inserts all inputs of every accepted pool transaction unconditionally with no eviction or independent cap. [2](#0-1) 

**Unbounded `NOT IN` construction in `get_cells`.**
Lines 110–135 of `get_cells.rs` iterate the entire `dead_cells` set, building one `($N, output_index)` placeholder per entry and appending the full list to the SQL `WHERE` clause via `query_builder.and_where(format!("(tx_hash, output_index) NOT IN ({})", placeholders))`. [3](#0-2)  Lines 221–225 then bind one `tx_hash` value per dead cell. [4](#0-3) 

**`request_limit` guard is irrelevant.**
Lines 25–34 cap the number of *result rows* returned, not the size of the `NOT IN` clause. The dead-cell expansion happens unconditionally before any row is fetched. [5](#0-4) 

**Same pattern in `get_cells_capacity`.**
Lines 73–102 of `get_cells_capacity.rs` replicate the identical unbounded `NOT IN` construction and bind loop at lines 174–178. [6](#0-5) [7](#0-6) 

**Ceiling from tx-pool size.**
`util/app-config/src/legacy/tx_pool.rs` line 20 sets `DEFAULT_MAX_TX_POOL_SIZE = 180_000_000` (180 MB). [8](#0-7)  At a minimum transaction size of ~200 bytes this permits ~900,000 pool transactions and a corresponding number of dead cells, yielding a SQL string of ~13 MB and ~28 MB of parameter data per RPC call.

## Impact Explanation
Every `get_cells` and `get_cells_capacity` RPC call incurs O(N) CPU work for SQL string construction, O(N) memory allocation for parameter binding, and O(N) query-plan work in the database, where N is the current dead-cell count. At pool saturation this renders the rich-indexer RPC effectively unresponsive for all callers. This matches the allowed impact: **Low (501–2000 points) — any other important performance improvements for CKB**, and at full saturation approaches **Note (0–500 points) — any local RPC API crash** (functional unresponsiveness).

## Likelihood Explanation
Requires `index_tx_pool = true` (opt-in but documented production feature) and enough pre-existing live UTXOs to fill the pool. The fee cost to submit 180 MB of transactions at the default minimum fee rate of 1000 shannons/KB is approximately 1.8 CKB — economically trivial. UTXO pre-creation is a one-time cost. Even a partial fill (e.g., 10,000 transactions) produces measurably increased latency on every `get_cells` call. The attack is repeatable and requires no special privileges beyond submitting standard transactions.

## Recommendation
Cap the number of dead cells included in the `NOT IN` clause to a fixed maximum (e.g., 1,000). If the live pool exceeds that cap, either omit the filter (accepting a small risk of transiently returning a just-spent cell) or return an explicit error to the caller. A more robust alternative is to store dead cells in a temporary database table and perform a `NOT EXISTS` / anti-join rather than expanding them inline into the query string.

## Proof of Concept
1. Configure the node with `index_tx_pool = true` in the indexer section.
2. Pre-create N distinct live cells on-chain (test with N = 1,000 / 10,000 / 100,000).
3. Submit N transactions to the tx-pool, each spending exactly one distinct live cell.
4. Call `get_cells` via RPC and measure: SQL string length (loggable at the `SQLXPool::new_query` call site), parameter count, and wall-clock latency.
5. Observe that SQL string length and latency grow linearly with N; at N ≈ 900,000 (pool saturation at 180 MB default) the RPC becomes effectively unresponsive.
6. Confirm the same behavior for `get_cells_capacity`.

### Citations

**File:** util/indexer-sync/src/pool.rs (L20-22)
```rust
pub struct Pool {
    dead_cells: HashSet<OutPoint>,
}
```

**File:** util/indexer-sync/src/pool.rs (L40-44)
```rust
    pub fn new_transaction(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.insert(input.previous_output());
        }
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L221-225)
```rust
        if !dead_cells.is_empty() {
            for (tx_hash, _) in dead_cells {
                query = query.bind(tx_hash)
            }
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L174-178)
```rust
        if !dead_cells.is_empty() {
            for (tx_hash, _) in dead_cells {
                query = query.bind(tx_hash)
            }
        }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L19-20)
```rust
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```
