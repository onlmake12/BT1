The code is confirmed. The `break` at line 232 is present in the actual source, `spend_cell` returns `Ok(false)` on zero rows affected (cell absent from DB), and the loop exits early — leaving subsequent inputs unprocessed.

Audit Report

## Title
`break` on Unindexed Input Silently Skips Remaining Inputs, Leaving Indexed Cells Permanently Unspent — (`util/rich-indexer/src/indexer/mod.rs`)

## Summary
In `AsyncRichIndexer::insert_transaction`, the input-processing loop uses `break` when `spend_cell` returns `Ok(false)`, which occurs whenever an input's out-point is absent from the indexer's `output` table. This exits the entire loop, so all subsequent inputs in the same transaction are never processed. Any indexed live cells referenced by those later inputs retain `is_spent = 0` permanently, causing the rich indexer's state to diverge from chain state.

## Finding Description
In `util/rich-indexer/src/indexer/mod.rs` lines 228–249, the input loop is:

```rust
if tx_index != 0 {
    for (input_index, input) in tx_view.inputs().into_iter().enumerate() {
        let out_point = input.previous_output();
        if !spend_cell(&out_point, tx).await? {
            break;   // exits entire loop
        }
        // build input_rows, mark is_tx_matched …
    }
}
```

`spend_cell` (`util/rich-indexer/src/indexer/insert.rs`, lines 403–427) issues:

```sql
UPDATE output SET is_spent = 1 WHERE tx_id = (...) AND output_index = $2
```

and returns `Ok(rows_affected > 0)`. It returns `Ok(false)` — zero rows affected — whenever the referenced out-point is **not present** in the `output` table. This is the normal, expected outcome for:

- Cells excluded by a configured `cell_filter` (Rhai script) — they were never inserted into `output`.
- Cells from blocks before the configured `init_tip_hash` — never indexed.

When `spend_cell` returns `Ok(false)` for `input[0]`, `break` terminates the loop. `input[1..N]` are never visited; `spend_cell` is never called for them; their rows in `output` keep `is_spent = 0`. The DB transaction is then committed with those cells still marked live (lines 169–171). The inconsistency is persisted to SQLite/PostgreSQL and survives restarts.

No existing guard prevents this: the only check is `tx_index != 0` (skipping coinbase), which is correct but unrelated.

## Impact Explanation
This is a correctness bug in the CKB rich indexer's state storage mechanism. After the consuming block is committed, any indexed cell referenced by `input[1..N]` remains permanently visible as a live cell via `get_cells` / `get_cells_capacity` RPCs. The indexer's view of live cells diverges from actual chain state. This matches the allowed impact: **Medium (2001–10000 points) — Suboptimal implementation of CKB state storage mechanism**, as the rich indexer is the SQL-backed cell state storage layer and its cell-spent tracking is permanently incorrect for affected outputs.

## Likelihood Explanation
The trigger requires a valid, consensus-accepted transaction whose inputs are ordered so that `input[0]` references a cell absent from the indexer DB. This is achievable by any user who:

1. Holds a cell excluded by the operator's `cell_filter` (a documented, common configuration), **and**
2. Also holds at least one indexed live cell.

They place the filtered/unindexed cell first in the input list. The CKB protocol imposes no ordering constraint on transaction inputs, so the ordering is fully attacker-controlled. No miner collusion or privileged access is required. The condition is also met naturally (without adversarial intent) whenever `init_tip_hash` is configured and a user spends a pre-init cell alongside an indexed cell.

## Recommendation
Replace `break` with `continue` at line 232 of `util/rich-indexer/src/indexer/mod.rs`:

```rust
if !spend_cell(&out_point, tx).await? {
    continue;   // cell not in indexer DB (filtered/pre-init-tip); skip, don't abort loop
}
```

This matches the semantics of the legacy RocksDB indexer (`util/indexer/src/indexer.rs`, lines 347–362), which uses `if let Some(stored_live_cell) = … { … }` — an implicit `continue` on miss — rather than aborting the loop.

## Proof of Concept
1. Start rich-indexer with `cell_filter` set to index only cells with a specific type script (e.g., TYPE-ID cells).
2. Append `block_0` containing:
   - cellbase (`tx_index=0`)
   - `tx_A` (`tx_index=1`): outputs `cell_X` (matches filter, indexed) and `cell_Y` (no type script, NOT indexed)
3. Append `block_1` containing:
   - cellbase
   - `tx_B` (`tx_index=1`): inputs = [`cell_Y` (unindexed, placed first), `cell_X` (indexed)]
4. Query: `SELECT is_spent FROM output WHERE …` for `cell_X`'s out-point.
   - **Expected**: `is_spent = 1`
   - **Actual**: `is_spent = 0` — indexer has diverged from chain state
5. `get_cells` RPC still returns `cell_X` as a live cell even though it is spent on-chain.