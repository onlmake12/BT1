The code is confirmed. The `break` at line 232 is real, `spend_cell` semantics are exactly as described, and the loop logic is exactly as claimed.

Audit Report

## Title
`break` on Unindexed Input Silently Skips Remaining Inputs, Leaving Indexed Cells Permanently Unspent — (`util/rich-indexer/src/indexer/mod.rs`)

## Summary

In `AsyncRichIndexer::insert_transaction`, the input-processing loop uses `break` when `spend_cell` returns `Ok(false)`, which occurs whenever an input's out-point is absent from the indexer's `output` table. This exits the entire loop, so all subsequent inputs in the same transaction are never processed. Any indexed live cells referenced by those later inputs retain `is_spent = 0` permanently, causing the rich indexer to diverge from chain state in a way that survives node restarts.

## Finding Description

In `util/rich-indexer/src/indexer/mod.rs` lines 228–249, the input loop is:

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

`spend_cell` (`util/rich-indexer/src/indexer/insert.rs`, lines 403–427) issues `UPDATE output SET is_spent = 1 WHERE …` and returns `Ok(rows_affected > 0)`. It returns `Ok(false)` — zero rows affected — whenever the referenced out-point is not present in the `output` table. This is the normal, expected outcome for cells filtered out by a configured `cell_filter`, cells from blocks before `init_tip_hash`, or genesis cells never inserted.

When `spend_cell` returns `Ok(false)` for `input[0]`, the `break` terminates the loop. `input[1..N]` are never visited; `spend_cell` is never called for them; their rows in `output` keep `is_spent = 0`. The DB transaction is then committed at lines 169–171 with those cells still marked live. The correct fix is `continue` (skip the unindexed input, process the rest), matching the legacy RocksDB indexer's `if let Some(stored_live_cell) = …` pattern which implicitly continues on a miss.

## Impact Explanation

This is a correctness bug in the CKB rich indexer's state storage mechanism. After the consuming block is committed, any indexed cell referenced by `input[1..N]` is permanently visible as a live cell via `get_cells` / `get_cells_capacity` RPCs. The inconsistency is persisted in SQLite/PostgreSQL and survives node restarts. This matches the allowed bounty impact: **Medium (2001–10000 points) — Suboptimal implementation of CKB state storage mechanism**, as the indexer's stored state permanently diverges from actual chain state for affected cells.

## Likelihood Explanation

The trigger requires only a valid, consensus-accepted transaction whose inputs are ordered so that `input[0]` references a cell absent from the indexer DB. This is achievable by any unprivileged user who holds a cell excluded by the operator's `cell_filter` (a documented, common configuration) and also holds at least one indexed live cell. They simply place the filtered cell first in the input list. The CKB protocol imposes no ordering constraint on transaction inputs, so the ordering is fully attacker-controlled. No miner collusion, privileged access, or key leakage is required.

## Recommendation

Replace `break` with `continue` at line 232 of `util/rich-indexer/src/indexer/mod.rs`:

```rust
if !spend_cell(&out_point, tx).await? {
    continue;   // cell not in indexer DB (filtered/pre-init-tip); skip, don't abort loop
}
```

This matches the semantics of the legacy RocksDB indexer (`util/indexer/src/indexer.rs`, lines 347–421), which uses `if let Some(stored_live_cell) = …` — an implicit `continue` on miss — rather than aborting the loop.

## Proof of Concept

```
1. Start rich-indexer with cell_filter that excludes cells without a type script
   (e.g., only TYPE-ID cells are indexed)

2. Append block_0 containing:
   - cellbase (tx_index=0)
   - tx_A (tx_index=1): outputs cell_X (matches filter, indexed) and cell_Y (no type, NOT indexed)

3. Append block_1 containing:
   - cellbase
   - tx_B (tx_index=1): inputs = [cell_Y (unindexed, first), cell_X (indexed, second)]
     spend_cell(cell_Y) → Ok(false) → break → cell_X never processed

4. Query: SELECT is_spent FROM output WHERE … (cell_X's out_point)
   Expected: is_spent = 1
   Actual:   is_spent = 0  ← indexer diverged from chain state

5. get_cells RPC still returns cell_X as a live cell even though it is spent on-chain.
```