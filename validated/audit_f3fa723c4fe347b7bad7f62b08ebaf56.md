The code is confirmed. Let me verify the timeout handling for the rich-indexer to complete the assessment.

Audit Report

## Title
Unbounded Pre-Aggregation in `get_tx_with_cells` Grouped Mode Allows RPC DoS — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

## Summary
When `group_by_transaction=true`, `get_tx_with_cells` constructs a three-level nested SQL query where `GROUP BY` with `GROUP_CONCAT`/`ARRAY_AGG` appears before `LIMIT`. Because `GROUP BY` with aggregate functions is a blocking operator in both SQLite and PostgreSQL, the database must fully materialize every matching row from the inner `UNION ALL` subquery before returning even a single grouped result. The `request_limit` guard only caps the caller-supplied `limit` value and does not bound the number of rows scanned internally. No timeout is applied to rich-indexer queries. A caller with RPC access can trigger unbounded CPU and I/O work with a single request.

## Finding Description
`get_tx_with_cells` (lines 271–430) builds the query in three levels:

**Level 1** — `build_tx_with_cell_union_sub_query` (line 279) produces a `UNION ALL` of all matching outputs and inputs. No `LIMIT` is applied at this level.

**Level 2** — Lines 281–299 join the union result with `ckb_transaction` and `block`, apply an optional `block_range` filter, and wrap the result as a subquery. Still no `LIMIT`.

**Level 3** — Lines 320–332 apply `WHERE tx_id > after`, then `GROUP BY tx_id, block_number, tx_index, tx_hash`, then `ORDER BY tx_id`, then `LIMIT limit`.

The `request_limit` guard at lines 27–32 only rejects calls where the caller-supplied `limit` exceeds the configured maximum. It does not limit the number of rows the inner subquery scans and aggregates. The `AsyncRichIndexerHandle` struct carries only `store`, `pool`, and `request_limit` — no `timeout_limit` field — and no query timeout is applied anywhere in the rich-indexer query path, confirmed by the absence of `timeout_limit` in the rich-indexer source. A call with `group_by_transaction=true`, `script_search_mode: "prefix"`, broad `args`, and `limit=1` forces the database to scan and aggregate every matching row before returning.

## Impact Explanation
This maps to **Note (0–500 points): Any local RPC API crash**. The rich-indexer RPC becomes effectively unresponsive for the duration of the unbounded query. Because the rich-indexer runs in the same process as the CKB node, sustained concurrent requests can exhaust database connection resources and degrade the indexer RPC. Core CKB node functions (consensus, P2P) are not directly affected, placing this below the threshold for a node crash finding.

## Likelihood Explanation
Any caller with RPC access can trigger this with a single valid JSON-RPC call. The default configuration binds to `127.0.0.1:8114` (localhost only), but many production deployments expose the RPC to the network. No authentication is required. The attack is repeatable with no rate limiting in the RPC layer. The repository's own stress-test script (`util/rich-indexer/src/tests/stress_test_scripts/get_transactions_prefix.lua`) demonstrates the exact call pattern with `group_by_transaction: true` and `script_search_mode: "prefix"`.

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

Additionally, apply the `timeout_limit` configuration to rich-indexer SQL queries, as it is currently absent from `AsyncRichIndexerHandle`.

## Proof of Concept
Run `EXPLAIN QUERY PLAN` on the generated SQL against a populated SQLite rich-indexer database with the query shown in the submission. The plan will show `USE TEMP B-TREE FOR GROUP BY` with no early termination, confirming full materialization before `LIMIT` is applied. Alternatively, use the existing stress-test script against a node with a populated index:

```
wrk -t4 -c30 -d30s -s ./util/rich-indexer/src/tests/stress_test_scripts/get_transactions_prefix.lua --latency http://127.0.0.1:8114
```

Measure latency difference between `group_by_transaction: true` and `group_by_transaction: false` on a dataset with 10,000+ matching transactions to observe the O(N) vs. O(limit) difference.