Audit Report

## Title
`break` on Unindexed Input Silently Skips Remaining Inputs, Leaving Indexed Cells Permanently Unspent — (`util/rich-indexer/src/indexer/mod.rs`)

## Summary

In `AsyncRichIndexer::insert_transaction`, the input-processing loop uses `break` when `spend_cell` returns `Ok(false)`, which occurs whenever an input's out-point is absent from the indexer's `output` table. This exits the entire loop, so all subsequent inputs in the same transaction are never processed. Any indexed live cells referenced by those later inputs retain `is_spent = 0` permanently, causing the rich indexer to diverge from chain state in a way that survives node restarts.

## Finding Description

In `util/rich-indexer/src/indexer/mod.rs` at lines 228–249, the input loop is:

```rust
if tx_index != 0 {
    for (input_index, input) in tx_view.inputs().into_iter().enumerate() {
        let out_point = input.previous_output();
        if !spend_cell(&out_point, tx).await? {
            break;   // exits the entire loop
        }
        // build input_rows, mark is_tx_matched …
    }
}
```

`spend_cell` (in `util/rich-indexer/src/indexer/insert.rs`, lines 403–427) issues:

```sql
UPDATE output SET is_spent = 1
WHERE tx_id = (SELECT id FROM ckb_transaction WHERE tx_hash = $1)
  AND output_index = $2
```

and returns `Ok(rows_affected > 0)`. It returns `Ok(false)` — zero rows affected — whenever the referenced out-point is **not present** in the `output` table. This is the normal, expected outcome for:

- Cells excluded by a configured `cell_filter` (Rhai script).
- Cells from blocks before the configured `init_tip_hash` (never indexed).
- Genesis cells that were never inserted into `output`.

When `spend_cell` returns `Ok(false)` for `input[0]`, the `break` terminates the loop. `input[1..N]` are never visited, so `spend_cell` is never called for them, and their rows in the `output` table keep `is_spent = 0`. The DB transaction is then committed with those cells still marked live.

The correct fix is `continue` (skip the unindexed input, process the rest), not `break`. This matches the semantics of the legacy RocksDB indexer (`util/indexer/src/indexer.rs`, lines 347–421), which uses `if let Some(stored_live_cell) = … { … }` — an implicit `continue` on miss — rather than aborting the loop.

## Impact Explanation

After the consuming block is committed, any indexed cell referenced by `input[1..N]` is permanently visible as a live cell via `get_cells` / `get_cells_capacity` RPCs. The inconsistency is persisted in SQLite/PostgreSQL and survives node restarts. This constitutes an incorrect implementation of the CKB state storage mechanism (the rich indexer), matching the **Medium (2001–10000 points)** bounty impact: *Suboptimal/incorrect implementation of CKB state storage mechanism*.

## Likelihood Explanation

The trigger requires only a valid, consensus-accepted transaction whose inputs are ordered so that `input[0]` references a cell absent from the indexer DB. This is trivially achievable by any unprivileged user who:

1. Holds a cell excluded by the operator's `cell_filter` (a documented, common configuration — e.g., index only TYPE-ID cells), **and**
2. Also holds at least one indexed live cell.

They simply place the filtered cell first in the input list. The CKB protocol imposes no ordering constraint on transaction inputs, so the ordering is fully attacker-controlled. No miner collusion, no privileged access, and no key leakage is required. The condition is repeatable across any block.

## Recommendation

Replace `break` with `continue` at line 232 of `util/rich-indexer/src/indexer/mod.rs`:

```rust
if !spend_cell(&out_point, tx).await? {
    continue;   // cell not in indexer DB (filtered/pre-init-tip); skip, don't abort loop
}
```

This matches the behavior of the legacy RocksDB indexer and correctly handles the case where an input's out-point is absent from the indexer.

## Proof of Concept

```
1. Start rich-indexer with cell_filter = "output.type?.args == \"0xdeadbeef\""
   (only TYPE-ID cells are indexed)

2. Append block_0 containing:
   - cellbase (tx_index=0)
   - tx_A (tx_index=1): outputs cell_X (matches filter, indexed) and cell_Y (no type, NOT indexed)

3. Append block_1 containing:
   - cellbase
   - tx_B (tx_index=1): inputs = [cell_Y (unindexed), cell_X (indexed)]
     (cell_Y first — spend_cell returns Ok(false) → break → cell_X never processed)

4. Query: SELECT is_spent FROM output WHERE … (cell_X's out_point)
   Expected: is_spent = 1
   Actual:   is_spent = 0  ← indexer diverged from chain state

5. get_cells RPC still returns cell_X as a live cell even though it is spent on-chain.
```