All four code citations check out against the actual source. Let me verify the one nuance in the placeholder construction before rendering a verdict.

All code citations verified against actual source. Rendering verdict.

---

Audit Report

## Title
Unbounded `NOT IN` SQL clause from `Pool.dead_cells` causes persistent `Error::DB` on `get_cells` and `get_cells_capacity` RPC — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`, `get_cells_capacity.rs`)

## Summary
When `index_tx_pool: true` is configured, `get_cells` and `get_cells_capacity` collect every entry from the unbounded `Pool.dead_cells` `HashSet` and embed one SQL bind parameter per entry into a `NOT IN (…)` clause with no upper bound. An attacker who floods the mempool with enough single-input transactions pushes the total bind-parameter count past SQLite's `SQLITE_MAX_VARIABLE_NUMBER`, causing every subsequent `get_cells` and `get_cells_capacity` call to return `Error::DB` for as long as the mempool remains large.

## Finding Description
`Pool.dead_cells` is a plain `HashSet<OutPoint>` with no capacity cap. [1](#0-0) 

`Pool::new_transaction` inserts every input of every accepted mempool transaction into that set with no guard. [2](#0-1) 

`get_cells` iterates the full set and constructs one `$N` placeholder per entry in a `NOT IN (…)` clause. The format `format!("(${}, {})", param_index, output_index)` inlines `output_index` as a literal but increments `param_index` (a bind slot for `tx_hash`) once per dead cell, so each dead cell consumes exactly one SQLite bind variable. [3](#0-2) 

`get_cells_capacity` has the identical pattern. [4](#0-3) 

The bind parameters are then appended in a second loop, confirming one bind slot per dead cell. [5](#0-4) 

`param_index` starts at 1 and is already advanced by the script sub-query parameters before the dead-cell loop begins, so the total bind count is (script params) + (dead-cell count). Once this total exceeds `SQLITE_MAX_VARIABLE_NUMBER`, SQLite returns a driver error that is propagated directly as `Error::DB`. [6](#0-5) 

There are no existing checks or guards anywhere in this code path that limit the size of `dead_cells` before query construction.

## Impact Explanation
All callers of the `get_cells` and `get_cells_capacity` RPC methods receive persistent `Error::DB` responses for the duration of the attack. The node's consensus and block-production paths are entirely unaffected; only the rich-indexer query surface is rendered non-functional. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**.

## Likelihood Explanation
- Requires `index_tx_pool: true` in the node configuration (non-default, but a documented and supported option).
- The threshold depends on the system SQLite version: pre-3.32.0 defaults to 999 variables; 3.32.0+ defaults to 32766. In either case, the required transaction count (≤32,000) is well within the 180 MB default `max_tx_pool_size`. [7](#0-6) 
- Submitting the required number of minimal single-input transactions requires no PoW, no privileged access, and negligible cost.
- The attack is repeatable and self-sustaining as long as the attacker's transactions remain in the mempool.

## Recommendation
Replace the inline `NOT IN (…)` expansion with a bounded approach:

1. **Temp table / CTE**: Insert dead cells into a temporary table or CTE and join against it — both SQLite and PostgreSQL support this without parameter-count limits.
2. **Chunked exclusion**: Split dead cells into chunks of ≤500 and apply multiple `NOT IN` clauses per chunk, keeping each chunk's bind count below the SQLite limit.
3. **Hard cap with clear error**: If `dead_cells().count()` exceeds a configurable threshold, return `Error::Params` immediately rather than attempting an oversized query.

## Proof of Concept
```rust
// Populate a mock Pool with enough dead cells to exceed SQLITE_MAX_VARIABLE_NUMBER
let mut pool = Pool::default();
for i in 0u32..1_000 {
    let tx = TransactionBuilder::default()
        .input(CellInput::new(OutPoint::new(h256!("0xdead").pack(), i), 0))
        .build();
    pool.new_transaction(&tx.into_view());
}
// pool.dead_cells().count() == 1,000
// get_cells() builds a NOT IN clause with 1,000 bind parameters (plus script params),
// exceeding SQLITE_MAX_VARIABLE_NUMBER on pre-3.32.0 SQLite → returns Error::DB.
// On 3.32.0+ SQLite (limit 32766), repeat with ~32,000 transactions — still feasible
// within the 180 MB default max_tx_pool_size.
```

With a node configured with `index_tx_pool = true` and SQLite backend, submit sufficient transactions each spending a distinct UTXO via `send_transaction` RPC, then call `get_cells` — it will return a DB error on every subsequent invocation until the mempool drains.

### Citations

**File:** util/indexer-sync/src/pool.rs (L19-22)
```rust
#[derive(Default)]
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

**File:** util/indexer-sync/src/pool.rs (L59-61)
```rust
    pub fn dead_cells(&self) -> impl Iterator<Item = &OutPoint> {
        self.dead_cells.iter()
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L159-163)
```rust
        let sql = query_builder
            .sql()
            .map_err(|err| Error::DB(err.to_string()))?
            .trim_end_matches(';')
            .to_string();
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
