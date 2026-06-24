The code confirms the claim. Let me check a bit more context to be thorough.

Audit Report

## Title
`break` Instead of `continue` in Input Loop Causes Incomplete Cell Spending in Rich-Indexer — (File: util/rich-indexer/src/indexer/mod.rs)

## Summary
In `AsyncRichIndexer::insert_transaction`, the loop over a transaction's inputs calls `spend_cell` to mark each consumed cell as spent in the SQL `output` table. When `spend_cell` returns `false` (cell absent from the DB), a `break` at line 232 terminates the entire loop, silently skipping all subsequent inputs. Cells that should be marked `is_spent = 1` remain as live cells in the rich-indexer's database, permanently corrupting the off-chain index until a full re-sync.

## Finding Description
In `util/rich-indexer/src/indexer/mod.rs` lines 228–248, `insert_transaction` iterates over all inputs of a non-coinbase transaction:

```rust
if tx_index != 0 {
    for (input_index, input) in tx_view.inputs().into_iter().enumerate() {
        let out_point = input.previous_output();
        if !spend_cell(&out_point, tx).await? {
            break;   // ← exits the entire loop
        }
        // ... build_input_rows ...
    }
}
```

`spend_cell` (in `util/rich-indexer/src/indexer/insert.rs`, lines 403–427) issues `UPDATE output SET is_spent = 1 WHERE tx_id = ... AND output_index = ...` and returns `Ok(updated_rows > 0)`. It returns `false` — not an error — whenever the referenced output is absent from the `output` table. This is a normal, expected condition when:
- The cell was produced by a transaction excluded by `custom_filters`.
- The indexer was initialized with `set_init_tip` after the cell was created.
- The cell originates from a genesis transaction not indexed.

When `spend_cell` returns `false` for input `i`, the `break` exits the loop. Inputs `i+1`, `i+2`, … are never visited; their cells are never marked spent. The rollback path (`reset_spent_cells` in `util/rich-indexer/src/indexer/remove.rs`, line 18) operates on the `tx_id_list` of the rolled-back block and cannot repair cells that were silently skipped during the forward append pass.

## Impact Explanation
**Medium (2001–10000 points) — Suboptimal implementation of CKB state storage mechanism.**

The rich-indexer is CKB's off-chain cell state storage mechanism. The bug causes the SQL `output` table to retain stale `is_spent = 0` rows for cells that have been consumed on-chain. Any consumer of the rich-indexer's `get_cells` or `get_transactions` RPC endpoints receives incorrect cell liveness data. The corruption is permanent until a full re-sync; rollback does not repair it.

## Likelihood Explanation
Any transaction with two or more inputs where the first input's cell is absent from the `output` table triggers the bug. This is routine in deployments using custom cell filters (a common configuration) or when the indexer was initialized mid-chain via `set_init_tip`. No special attacker capability is required — any unprivileged transaction sender submitting a multi-input transaction can trigger this path.

## Recommendation
Replace `break` with `continue` at line 232 of `util/rich-indexer/src/indexer/mod.rs`:

```rust
if !spend_cell(&out_point, tx).await? {
    continue;   // cell not in DB, skip but keep processing remaining inputs
}
```

This ensures all inputs are visited regardless of whether earlier inputs' cells are present in the database.

## Proof of Concept
1. Configure the rich-indexer with a custom cell filter that excludes cell type `X`.
2. Submit a transaction `T` with two inputs:
   - **Input 0**: spends cell `A` of type `X` (excluded by filter → absent from `output` table → `spend_cell` returns `false` → `break` fires).
   - **Input 1**: spends cell `B` of a tracked type (present in `output` table → never reached).
3. After `insert_transaction` returns, query the DB: `SELECT is_spent FROM output WHERE ...` for cell `B` → returns `0`.
4. Call `get_cells` for the script locking cell `B` → cell `B` is returned as a live cell despite being consumed on-chain.