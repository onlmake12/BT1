All claims verified against the actual code. The three-level query structure, missing `timeout_limit`, and stress-test script all confirm exactly as described.

Audit Report

## Title
Unbounded Pre-Aggregation in `get_tx_with_cells` Grouped Mode Allows RPC DoS — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

## Summary
When `group_by_transaction=true`, `get_tx_with_cells` builds a three-level nested SQL query where `GROUP BY` with `GROUP_CONCAT`/`ARRAY_AGG` is applied before `LIMIT`. Both SQLite and PostgreSQL must fully materialize every matching row from the inner `UNION ALL` before returning even one grouped result. The `request_limit` guard only rejects oversized caller-supplied `limit` values and does not bound internal scan work. No query timeout exists in the rich-indexer path. A single RPC call can trigger unbounded CPU and I/O work.

## Finding Description
`get_tx_with_cells` (lines 271–430) constructs the query in three levels:

**Level 1** — `build_tx_with_cell_union_sub_query` (line 279) emits a `UNION ALL` of all matching outputs and inputs. No `LIMIT` is applied. [1](#0-0) 

**Level 2** — Lines 281–299 join the union result with `ckb_transaction` and `block`, apply an optional `block_range` filter, and wrap the result as a subquery. Still no `LIMIT`. [2](#0-1) 

**Level 3** — Lines 320–332 apply `WHERE tx_id > after`, then `GROUP BY tx_id, block_number, tx_index, tx_hash` with `GROUP_CONCAT`/`ARRAY_AGG`, then `ORDER BY tx_id`, then `LIMIT limit`. The database must fully aggregate all matching rows before `LIMIT` can be applied. [3](#0-2) 

The `request_limit` guard at lines 27–32 only rejects calls where the caller-supplied `limit` exceeds the configured maximum. It does not bound the number of rows the inner subquery scans and aggregates. [4](#0-3) 

`AsyncRichIndexerHandle` carries only `store`, `pool`, and `request_limit` — no `timeout_limit` field — confirming no query timeout is applied anywhere in the rich-indexer query path. [5](#0-4) 

A grep for `timeout_limit` across all of `util/rich-indexer/` returns zero matches, while the same identifier exists in `util/indexer/src/service.rs` and `util/app-config/src/configs/indexer.rs`, confirming the timeout mechanism was never wired into the rich-indexer. [6](#0-5) 

## Impact Explanation
The rich-indexer RPC becomes effectively unresponsive for the duration of the unbounded query. Because the rich-indexer runs in the same process as the CKB node, sustained concurrent requests can exhaust database connection resources and degrade the indexer RPC. Core CKB node functions (consensus, P2P) are not directly affected. This matches **Note (0–500 points): Any local RPC API crash**.

## Likelihood Explanation
Any caller with RPC access can trigger this with a single valid JSON-RPC call using `group_by_transaction: true`, `script_search_mode: "prefix"`, broad `args`, and `limit=1`. The default configuration binds to `127.0.0.1:8114` (localhost only), but many production deployments expose the RPC to the network. No authentication is required. The attack is repeatable with no rate limiting in the RPC layer. The repository's own stress-test script demonstrates the exact call pattern. [7](#0-6) 

## Recommendation
Restructure the grouped query to select the top-N distinct `tx_id` values (with `ORDER BY` and `LIMIT`) before aggregation, then join back to aggregate only those N transactions' cells. A CTE or derived subquery approach:

```sql
WITH paged_txs AS (
  SELECT DISTINCT tx_id FROM (...union subquery...) AS u
  [WHERE tx_id > ?after]
  ORDER BY tx_id ASC
  LIMIT ?limit
)
SELECT p.tx_id, b.block_number, t.tx_index, t.tx_hash,
       GROUP_CONCAT(u.io_type||','||u.io_index) AS io_pairs
FROM paged_txs p
JOIN (...union subquery...) AS u ON u.tx_id = p.tx_id
JOIN ckb_transaction t ON t.id = p.tx_id
JOIN block b ON b.id = t.block_id
GROUP BY p.tx_id, b.block_number, t.tx_index, t.tx_hash
ORDER BY p.tx_id ASC;
```

Additionally, wire the `timeout_limit` configuration into `AsyncRichIndexerHandle` and apply it to all SQL queries, as it is currently absent from the rich-indexer query path entirely.

## Proof of Concept
Run `EXPLAIN QUERY PLAN` on the generated SQL against a populated SQLite rich-indexer database. The plan will show `USE TEMP B-TREE FOR GROUP BY` with no early termination, confirming full materialization before `LIMIT` is applied. Alternatively, use the existing stress-test script against a node with a populated index:

```
wrk -t4 -c30 -d30s -s ./util/rich-indexer/src/tests/stress_test_scripts/get_transactions_prefix.lua --latency http://127.0.0.1:8114
```

Measure latency difference between `group_by_transaction: true` and `group_by_transaction: false` on a dataset with 10,000+ matching transactions to observe the O(N) vs. O(limit) difference.

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L27-32)
```rust
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L279-279)
```rust
    let sql_union = build_tx_with_cell_union_sub_query(db_driver, &search_key)?;
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L281-299)
```rust
    let mut query_builder = SqlBuilder::select_from(format!("{} AS res_union", sql_union));
    query_builder
        .field("tx_id, block.block_number, ckb_transaction.tx_index, ckb_transaction.tx_hash, io_type, io_index")
        .join("ckb_transaction")
        .on("res_union.tx_id = ckb_transaction.id")
        .join("block")
        .on("ckb_transaction.block_id = block.id");

    if let Some(filter) = &search_key.filter
        && let Some(block_range) = &filter.block_range
    {
        query_builder.and_where_ge("block.block_number", block_range.start());
        query_builder.and_where_lt("block.block_number", block_range.end());
    }
    let sql = query_builder
        .subquery()
        .map_err(|err| Error::DB(err.to_string()))?
        .trim_end_matches(';')
        .to_string();
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L320-332)
```rust
    if let Some(after) = after {
        let after = decode_i64(after.as_bytes())?;
        match order {
            IndexerOrder::Asc => query_builder.and_where_gt("tx_id", after),
            IndexerOrder::Desc => query_builder.and_where_lt("tx_id", after),
        };
    }
    query_builder.group_by("tx_id, block_number, tx_index, tx_hash");
    match order {
        IndexerOrder::Asc => query_builder.order_by("tx_id", false),
        IndexerOrder::Desc => query_builder.order_by("tx_id", true),
    };
    query_builder.limit(limit);
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L23-27)
```rust
pub struct AsyncRichIndexerHandle {
    store: SQLXPool,
    pool: Option<Arc<RwLock<Pool>>>,
    request_limit: usize,
}
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L29-37)
```rust
impl AsyncRichIndexerHandle {
    /// Construct new AsyncRichIndexerHandle instance
    pub fn new(store: SQLXPool, pool: Option<Arc<RwLock<Pool>>>, request_limit: usize) -> Self {
        Self {
            store,
            pool,
            request_limit,
        }
    }
```

**File:** util/rich-indexer/src/tests/stress_test_scripts/get_transactions_prefix.lua (L1-24)
```lua
wrk.method = "POST"
wrk.headers["Content-Type"] = "application/json"

wrk.body = [[
{
    "id": 2,
    "jsonrpc": "2.0",
    "method": "get_transactions",
    "params": [
        {
            "script": {
                "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
                "hash_type": "type",
                "args": "0x5989ae415b"
            },
            "script_type": "lock",
            "script_search_mode": "prefix",
            "group_by_transaction": true
        },
        "asc",
        "0x64"
    ]
}
]]
```
