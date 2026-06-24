The code matches the claim exactly. At `mod.rs` line 232, `break` exits the entire input loop when `spend_cell` returns `false`. At `insert.rs` line 426, `spend_cell` returns `false` when `rows_affected == 0` (cell absent from DB due to filtering). The bug is real and reproducible.

Assessing impact against the allowed scope: this is a correctness defect in the rich-indexer's state storage — `is_spent` is permanently wrong for indexed cells that follow a filtered cell in the same transaction's input list. This fits "Suboptimal implementation of CKB state storage mechanism" (Medium).

---

Audit Report

## Title
`insert_transaction` input loop `break` on filtered cell leaves subsequent indexed cells permanently `is_spent=0` — (`util/rich-indexer/src/indexer/mod.rs`)

## Summary
In `AsyncRichIndexer::insert_transaction`, the loop over a transaction's inputs issues a `break` whenever `spend_cell` returns `false`. `spend_cell` returns `false` for any input whose referenced output is absent from the DB (filtered out by `cell_filter`). All inputs after the first filtered-out one are silently skipped, so their `output` rows are never updated to `is_spent = 1`. The stale records persist across restarts and are returned as live cells by every RPC that queries on `is_spent = 0`.

## Finding Description
`spend_cell` (`insert.rs` L410–426) executes `UPDATE output SET is_spent = 1 WHERE tx_id = ... AND output_index = ...` and returns `Ok(rows_affected > 0)`. When the referenced output was excluded by `cell_filter` it was never inserted, so `rows_affected == 0` and the function returns `Ok(false)`. [1](#0-0) 

The caller at `mod.rs` L231–233 treats this `false` as a signal to `break` the entire input loop: [2](#0-1) 

Any input at position `i+1, i+2, …` after the first filtered-out input is never visited. Their `output` rows retain `is_spent = 0` indefinitely. The subsequent `query_output_cell` / `query_output_id` path that would have called `build_input_rows` is also skipped, so the input table is also incomplete.

No existing guard compensates: the `is_cell_filter_enabled` branch (L234–243) is only reached after the `break` check, and there is no post-loop reconciliation pass.

## Impact Explanation
The rich-indexer is CKB's local state storage mechanism. A persistent `is_spent = 0` on a spent cell means every `get_cells` (and related) RPC call returns that cell as live. Wallets, DEXes, and bridges that rely on the indexer for UTXO state will observe phantom live cells — incorrect balance displays, failed transaction construction, and incorrect application-level UTXO accounting. The corruption survives node restart and affects all RPC consumers of that node. This matches **Medium — Suboptimal implementation of CKB state storage mechanism (2001–10000 points)**.

## Likelihood Explanation
The trigger is a standard, valid CKB transaction with two or more inputs where the first input's cell was excluded by `cell_filter` (e.g., a plain-lock cell with no type script) and at least one later input is indexed (e.g., a type-script cell). This is a routine pattern for any user who holds both filtered and indexed cells. No miner privilege, no PoW, no special network position is required — only the ability to submit a normal transaction. The condition is repeatable and deterministic.

## Recommendation
Replace `break` with `continue` so that filtered-out inputs are skipped without aborting the processing of subsequent inputs:

```rust
// util/rich-indexer/src/indexer/mod.rs, inside the input loop
if !spend_cell(&out_point, tx).await? {
    continue;   // was: break
}
``` [3](#0-2) 

## Proof of Concept
1. Configure rich-indexer with a `cell_filter` that indexes only cells with a type script.
2. Create Cell A (lock-only, no type script — filtered out by `cell_filter`) and Cell B (has type script — indexed). Both owned by the same key.
3. Submit a valid transaction: `inputs = [Cell A, Cell B]`, spending both.
4. Wait for the block to be confirmed and the indexer to process it.
5. Query the `output` table directly: Cell B's row has `is_spent = 0`.
6. Call `get_cells` for Cell B's lock/type script: it is returned as a live cell despite being spent on-chain.
7. Restart the node — the stale record persists.

### Citations

**File:** util/rich-indexer/src/indexer/insert.rs (L424-426)
```rust
    .rows_affected();

    Ok(updated_rows > 0)
```

**File:** util/rich-indexer/src/indexer/mod.rs (L229-248)
```rust
            for (input_index, input) in tx_view.inputs().into_iter().enumerate() {
                let out_point = input.previous_output();
                if !spend_cell(&out_point, tx).await? {
                    break;
                }
                if self.custom_filters.is_cell_filter_enabled() {
                    if let Some((output_id, output, output_data)) =
                        query_output_cell(&out_point, tx).await?
                        && self
                            .custom_filters
                            .is_cell_filter_match(&output, &output_data.into())
                    {
                        build_input_rows(output_id, &input, input_index, &mut input_rows);
                        is_tx_matched = true;
                    }
                } else if let Some(output_id) = query_output_id(&out_point, tx).await? {
                    build_input_rows(output_id, &input, input_index, &mut input_rows);
                    is_tx_matched = true;
                }
            }
```
