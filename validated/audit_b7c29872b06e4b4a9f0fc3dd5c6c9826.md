The vulnerability is real. Here is the analysis:

---

### Title
Unbounded Pre-Aggregation in `get_tx_with_cells` Grouped Mode Allows Unprivileged RPC DoS — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

### Summary

When `group_by_transaction=true`, the `get_transactions` RPC generates a three-level nested SQL query where `LIMIT` is placed after `GROUP BY`. Because `GROUP BY` is a blocking aggregation operator, the database engine must fully materialize every matching row from the inner `UNION ALL` subquery before it can return even a single grouped result. An unprivileged caller can trigger this with a broad prefix search against a common lock script, causing the node to perform unbounded work regardless of the requested `limit`.

### Finding Description

The `get_tx_with_cells` function in `get_transactions.rs` constructs the following three-level query:

**Level 1 (innermost):** `build_tx_with_cell_union_sub_query` produces a `UNION ALL` of all outputs and inputs matching the search script. This subquery has **no LIMIT**. [1](#0-0) 

**Level 2 (middle):** Joins the union result with `ckb_transaction` and `block`, applies an optional `block_range` filter, and becomes a subquery. Still **no LIMIT**. [2](#0-1) 

**Level 3 (outer):** Applies `WHERE tx_id > after` (pagination cursor), then `GROUP BY tx_id, block_number, tx_index, tx_hash` with `GROUP_CONCAT`/`ARRAY_AGG`, then `ORDER BY tx_id`, then `LIMIT limit`. [3](#0-2) 

The resulting SQL structure is:
```sql
SELECT tx_id, ..., GROUP_CONCAT(io_type||','||io_index) AS io_pairs
FROM (
  SELECT ... FROM (
    SELECT tx_id, 1, output_index FROM output JOIN query_script ...
    UNION ALL
    SELECT consumed_tx_id, 0, input_index FROM input JOIN output JOIN query_script ...
  ) AS res_union
  JOIN ckb_transaction ... JOIN block ...
  [WHERE block_range]
) AS res
[WHERE tx_id > ?after]
GROUP BY tx_id, block_number, tx_index, tx_hash
ORDER BY tx_id ASC
LIMIT 1;  -- even limit=1 forces full aggregation
```

In both SQLite and PostgreSQL, `GROUP BY` is a blocking operator: the engine must consume **all** rows from `res` before emitting the first grouped row. `LIMIT` is applied only after all groups are formed. There is no mechanism for the planner to push `LIMIT` past `GROUP BY` when aggregation functions like `GROUP_CONCAT`/`ARRAY_AGG` are present.

The `request_limit` guard only caps the caller-supplied `limit` value: [4](#0-3) 

It does **not** limit the number of rows the inner subquery scans and aggregates.

### Impact Explanation

On a mainnet-scale node with the rich-indexer enabled, a common lock script (e.g., the default secp256k1 lock `0x9bd7e06f...`) appears in millions of cells across hundreds of thousands of transactions. A single RPC call with `group_by_transaction=true`, `script_search_mode: "prefix"`, `args: "0x"`, and `limit=1` forces the database to:
1. Scan and join every matching output and input row (potentially millions).
2. Aggregate all of them with `GROUP_CONCAT`/`ARRAY_AGG` into groups.
3. Sort all groups.
4. Only then apply `LIMIT 1`.

This causes severe CPU and I/O load on the indexer database, blocking other queries and degrading node responsiveness. The attack is repeatable with no rate limiting in the RPC layer.

### Likelihood Explanation

- The rich-indexer RPC is exposed to any network-accessible caller when enabled.
- No authentication is required.
- The attack payload is a single valid JSON-RPC call.
- The stress test script in the repository itself demonstrates this exact call pattern: [5](#0-4) 

### Recommendation

Restructure the grouped query to apply `LIMIT` on distinct `tx_id` values **before** aggregation. One approach: use a subquery or CTE to first select the top-N `tx_id` values (with `ORDER BY` and `LIMIT`), then join back to aggregate only those N transactions' cells:

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

This ensures the database only aggregates cells for the N requested transactions.

### Proof of Concept

Run `EXPLAIN QUERY PLAN` on the generated SQL against a populated SQLite rich-indexer database:

```sql
EXPLAIN QUERY PLAN
SELECT tx_id, block_number, tx_index, tx_hash,
  '"' || GROUP_CONCAT(io_type || ',' || io_index, '","') || '"' AS io_pairs
FROM (
  SELECT tx_id, block.block_number, ckb_transaction.tx_index, ckb_transaction.tx_hash, io_type, io_index
  FROM (
    SELECT output.tx_id AS tx_id, 1 AS io_type, output.output_index AS io_index
    FROM output JOIN script AS query_script ON output.lock_script_id = query_script.id
    WHERE query_script.code_hash = X'9bd7e06f...' AND query_script.hash_type = 1
      AND query_script.args >= X'' AND query_script.args < X'ff'
    UNION ALL
    SELECT input.consumed_tx_id AS tx_id, 0 AS io_type, input.input_index AS io_index
    FROM input JOIN output ON output.id = input.output_id
    JOIN script AS query_script ON output.lock_script_id = query_script.id
    WHERE query_script.code_hash = X'9bd7e06f...' AND query_script.hash_type = 1
      AND query_script.args >= X'' AND query_script.args < X'ff'
  ) AS res_union
  JOIN ckb_transaction ON res_union.tx_id = ckb_transaction.id
  JOIN block ON ckb_transaction.block_id = block.id
) AS res
GROUP BY tx_id, block_number, tx_index, tx_hash
ORDER BY tx_id ASC
LIMIT 1;
```

The plan will show `USE TEMP B-TREE FOR GROUP BY` with no early termination, confirming full materialization before `LIMIT` is applied. Benchmark grouped vs. ungrouped latency on a dataset with 10,000+ matching transactions to observe the O(N) vs. O(limit) difference.

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L23-32)
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L279-299)
```rust
    let sql_union = build_tx_with_cell_union_sub_query(db_driver, &search_key)?;

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
