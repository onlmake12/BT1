All three code locations are confirmed in the repository. Let me verify the specific claims:

**`Pool::new_transaction` — `pool.rs` L40–44:** [1](#0-0) 
Confirmed: inputs are inserted into `dead_cells` with no size cap or eviction policy.

**`get_cells` — `get_cells.rs` L110–135:** [2](#0-1) 
Confirmed: the entire `dead_cells` set is drained into a single `NOT IN (…)` clause with one `$N` placeholder per entry (only `tx_hash` is bound; `output_index` is inlined). No limit on placeholder count.

**`get_cells_capacity` — `get_cells_capacity.rs` L88–102:** [3](#0-2) 
Confirmed: identical unbounded NOT IN construction.

---

Audit Report

## Title
Unbounded `dead_cells` NOT IN Clause Causes Indexer RPC DoS via Tx Pool Flooding — (`util/indexer-sync/src/pool.rs`, `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`, `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

## Summary
When `index_tx_pool=true`, `Pool::new_transaction` inserts every input of every accepted pending transaction into an unbounded `HashSet<OutPoint>` with no size cap. Both `get_cells` and `get_cells_capacity` drain the entire set into a single SQL `NOT IN (…)` clause with one bound parameter per dead cell. SQLite rejects queries exceeding 999 bound variables; PostgreSQL rejects queries exceeding 65 535 parameters. An attacker who submits enough valid pending transactions can push the parameter count past either threshold, causing every subsequent `get_cells` / `get_cells_capacity` RPC call to return a DB error for as long as those transactions remain pending.

## Finding Description
**Root cause — `Pool::new_transaction` (`pool.rs` L40–44):** Every input of every accepted pending transaction is inserted into `dead_cells` with no cap or eviction policy. The `HashSet<OutPoint>` grows without bound as pending transactions accumulate.

**Trigger — `get_cells` (`get_cells.rs` L110–135):** All entries in `dead_cells` are collected into a `Vec`, then a single `NOT IN (…)` clause is appended to the SQL string. Each dead cell contributes exactly one bound parameter (`$N` for `tx_hash`; `output_index` is inlined into the SQL string). There is no limit on how many placeholders are emitted.

**Same pattern — `get_cells_capacity` (`get_cells_capacity.rs` L88–102):** Identical unbounded NOT IN construction using `(ckb_transaction.tx_hash, output_index) NOT IN (…)`.

**Why existing checks fail:** The `request_limit` field only caps the number of rows returned by the query; it does not limit the NOT IN clause size. `TxPoolConfig.max_tx_pool_size` limits pool size in bytes, not the total number of inputs across all pending transactions. A transaction spending 10 inputs is only a few KB, so thousands of inputs can accumulate well within the default byte limit. No guard exists between pool acceptance and NOT IN clause construction.

## Impact Explanation
Once `dead_cells` exceeds 999 entries (SQLite) or 65 535 entries (PostgreSQL), every call to `get_cells` and `get_cells_capacity` returns a DB error. The indexer is effectively unavailable for live-cell queries for the duration the offending transactions remain pending. This matches the allowed bounty impact: **Note (0–500 points) — Any local RPC API crash.**

## Likelihood Explanation
An attacker needs only to own ≥1 000 live UTXOs (achievable by splitting funds with cheap transactions) and submit ~100 pending transactions each spending ~10 inputs. This is well within the default pool byte-size limit and requires no privileged access — only standard `send_transaction` RPC calls. The attacker's funds are not lost; they remain in pending transactions, making the economic cost low and the attack repeatable.

## Recommendation
1. **Cap the NOT IN clause before building it:** Truncate `dead_cells` to a safe maximum (e.g., 500 for SQLite, 30 000 for PostgreSQL) before constructing the placeholder list in `get_cells` and `get_cells_capacity`.
2. **Use a temporary table or CTE:** Insert dead cells into a temporary table and JOIN against it instead of using a variable-length NOT IN list, eliminating the parameter-count dependency entirely.
3. **Enforce a hard limit on `Pool.dead_cells`:** In `Pool::new_transaction`, reject or evict entries once the set exceeds a configurable threshold, and expose that threshold as a node operator setting.

## Proof of Concept
1. Enable `index_tx_pool = true` in the indexer config.
2. Pre-fund a wallet with ≥1 000 live cells (achievable via fund-splitting transactions).
3. Submit 100 valid pending transactions via `send_transaction`, each spending 10 distinct inputs → `Pool.dead_cells` grows to 1 000 entries.
4. Call `get_cells` with any valid search key.
5. **Expected result (SQLite):** the node returns a DB error such as `"too many SQL variables"` because the generated NOT IN clause contains 1 000 `$N` placeholders, exceeding SQLite's 999-variable limit.
6. The error persists for every `get_cells` / `get_cells_capacity` call until the pending transactions are committed or expire per `expiry_hours`.

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
