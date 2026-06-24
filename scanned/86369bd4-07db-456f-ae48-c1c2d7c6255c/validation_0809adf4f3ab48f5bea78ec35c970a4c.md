The code is confirmed. Let me verify the exact lines and the legacy indexer behavior before rendering a verdict.

All code references are confirmed. The `break` at line 232 is real, `spend_cell` returning `false` for unindexed outputs is confirmed, and the legacy indexer's `continue`-via-`if let Some` contrast is verified.

Audit Report

## Title
Silent Input Skip via `break` on Unindexed Cell in `insert_transaction` — (`util/rich-indexer/src/indexer/mod.rs`)

## Summary

In `AsyncRichIndexer::insert_transaction`, the input-processing loop calls `spend_cell()` and immediately `break`s if it returns `false`. When a `cell_filter` is configured, outputs that do not match the filter are never stored in the `output` table, so `spend_cell()` returns `false` for any input referencing such a filtered-out output. A valid on-chain transaction whose first input references a filtered-out output will cause the loop to abort, leaving every subsequent input unprocessed — their referenced outputs are never marked `is_spent=1`. The result is permanent stale state in the indexer: indexed cells that are spent on-chain are returned as live by `get_cells`.

## Finding Description

In `util/rich-indexer/src/indexer/mod.rs` lines 228–248:

```rust
if tx_index != 0 {
    for (input_index, input) in tx_view.inputs().into_iter().enumerate() {
        let out_point = input.previous_output();
        if !spend_cell(&out_point, tx).await? {
            break;   // ← aborts the entire loop
        }
        // ... build input rows
    }
}
```

`spend_cell` in `util/rich-indexer/src/indexer/insert.rs` lines 403–427 issues `UPDATE output SET is_spent = 1 WHERE ...` and returns `Ok(updated_rows > 0)`. When the referenced output was never inserted into the `output` table (because it was excluded by a `cell_filter`), `updated_rows` is 0 and `spend_cell` returns `false`. The `break` then terminates the loop, so every input after the first unindexed one is silently skipped — their outputs are never marked spent.

The legacy KV-based indexer in `util/indexer/src/indexer.rs` lines 347–420 handles the identical situation correctly: the `if let Some(stored_live_cell) = self.store.get(...)` block simply falls through (implicit `continue`) when the cell is absent from the store, and the loop proceeds to the next input. The rich indexer's `break` is a direct behavioral divergence from this correct reference implementation.

`CustomFilters` in `util/indexer-sync/src/custom_filters.rs` lines 11–15 supports both `block_filter` and `cell_filter` as first-class, documented operator configurations. When either is active, outputs that do not match are never inserted into the `output` table, making the `spend_cell` false-return path reachable in normal production deployments.

## Impact Explanation

This is a **Medium** severity finding: **Suboptimal/incorrect implementation of CKB state storage mechanism** (2001–10000 points).

The rich indexer is a CKB state storage mechanism. The bug causes permanent incorrect state: indexed cells whose spending transaction contains an unindexed cell at `input[0]` will retain `is_spent=0` indefinitely. The `get_cells` RPC will return these cells as live UTXOs for all consumers of the affected node. No subsequent block processing corrects this — the corruption is self-reinforcing. Downstream applications (wallets, dApps) that rely on `get_cells` to enumerate spendable UTXOs will operate on permanently stale data. The actual on-chain consensus state is unaffected, but the indexer's UTXO view is durably corrupted for the lifetime of the node.

## Likelihood Explanation

The precondition is that the operator has configured a `cell_filter` — a documented, common deployment pattern (e.g., indexing only cells with a specific type script). Once that configuration is in place, any unprivileged on-chain participant can trigger the bug by submitting a valid transaction that spends an unindexed cell as `input[0]` alongside an indexed cell as `input[1]`. No special privilege is required; the transaction is fully valid from the consensus perspective. The bug fires on every such transaction and is permanent.

## Recommendation

Replace `break` with `continue` at line 232 of `util/rich-indexer/src/indexer/mod.rs`:

```rust
if !spend_cell(&out_point, tx).await? {
    continue;   // output not indexed; skip this input, process the rest
}
```

This matches the behavior of the legacy indexer and ensures all inputs are processed regardless of whether any individual input's referenced output is absent from the DB.

## Proof of Concept

1. Start the rich indexer with a `cell_filter` matching only cells with `type_script1`.
2. Append **Block 1** containing `tx_A` with two outputs: `cell_X` (no type script → filtered out, not inserted into `output` table) and `cell_Y` (has `type_script1` → inserted, `is_spent=0`).
3. Append **Block 2** containing `tx_B` with inputs `[cell_X, cell_Y]` (valid on-chain spend of both).
4. During `insert_transaction` for `tx_B`:
   - `spend_cell(cell_X)` → 0 rows updated → returns `false` → `break`.
   - `spend_cell(cell_Y)` is **never called**.
5. Assert: `SELECT is_spent FROM output WHERE ...` for `cell_Y` → **0** (should be 1).
6. Call `get_cells` for `cell_Y`'s lock script → it is returned as a live cell despite being spent on-chain.