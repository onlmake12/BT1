The code is present and I can trace the exact logic. Let me confirm the precise lines.

The bug is real and concrete. Here is the full analysis:

---

### Title
Rich-Indexer `insert_transaction` breaks input loop on first filtered-out cell, leaving subsequent indexed cells permanently marked `is_spent=0` — (`util/rich-indexer/src/indexer/mod.rs`)

### Summary

In `AsyncRichIndexer::insert_transaction`, when iterating over a transaction's inputs, a `break` is issued the moment `spend_cell` returns `false`. `spend_cell` returns `false` whenever the referenced output is absent from the DB (i.e., it was filtered out by `cell_filter`). Any subsequent inputs in the same transaction — including ones that **are** indexed — are never processed, so their `is_spent` flag is never set to `1`. The result is a persistent stale live-cell record visible to all RPC callers.

### Finding Description

**`spend_cell` return value semantics** (`insert.rs` lines 403–427):

`spend_cell` issues `UPDATE output SET is_spent = 1 WHERE ...` and returns `Ok(rows_affected > 0)`. When the output is absent from the DB (filtered out), `rows_affected == 0`, so it returns `Ok(false)`. [1](#0-0) 

**The broken loop** (`mod.rs` lines 228–248):

```rust
for (input_index, input) in tx_view.inputs().into_iter().enumerate() {
    let out_point = input.previous_output();
    if !spend_cell(&out_point, tx).await? {
        break;          // ← exits the entire loop
    }
    // ... build_input_rows for indexed inputs ...
}
``` [2](#0-1) 

When `spend_cell` returns `false` for input `i`, the `break` exits the loop entirely. Inputs `i+1, i+2, …` are never visited. Their corresponding DB rows keep `is_spent = 0`.

**Trigger condition**: a valid on-chain transaction whose input list is ordered `[filtered_cell, indexed_cell, ...]`. This ordering is fully under the control of the transaction author — no miner privilege is required.

### Impact Explanation

- Any indexed output that appears after a filtered-out output in the same transaction's input list will permanently retain `is_spent = 0` in the `output` table.
- `get_cells` (and any RPC that queries live cells) will return these already-spent cells as live, because the standard live-cell query filters on `is_spent = 0`.
- The corruption is persistent (survives node restart) and affects all RPC callers of that node.
- Downstream applications (wallets, DEXes, bridges) that rely on the indexer for UTXO state will observe phantom live cells, potentially leading to failed transactions, incorrect balance displays, or exploitable double-spend assumptions in application logic.

### Likelihood Explanation

The trigger requires only a normal, valid CKB transaction with two or more inputs where the first input's cell was excluded by the configured `cell_filter` (e.g., a plain-lock cell with no type script) and at least one later input's cell is indexed (e.g., a type-script cell). This is a routine pattern for any user who holds both filtered and indexed cells. No PoW, no privileged role, no special network position is needed — only the ability to submit a standard transaction.

### Recommendation

Replace `break` with `continue` on the `spend_cell` false-return branch so that filtered-out inputs are skipped without aborting the processing of subsequent inputs:

```rust
if !spend_cell(&out_point, tx).await? {
    continue;   // was: break
}
``` [3](#0-2) 

### Proof of Concept

1. Configure rich-indexer with a `cell_filter` that only indexes cells with a type script.
2. Create Cell A (lock-only, no type script — filtered out) and Cell B (has type script — indexed). Both owned by the same key.
3. Submit a valid transaction: `inputs = [Cell A, Cell B]`, spending both.
4. Wait for the transaction to be confirmed and the indexer to process the block.
5. Query the `output` table for Cell B's row: `is_spent` is `0`.
6. Call `get_cells` for Cell B's lock/type script: it is returned as a live cell despite being spent on-chain.

### Citations

**File:** util/rich-indexer/src/indexer/insert.rs (L410-426)
```rust
    let updated_rows = sqlx::query(
        r#"
            UPDATE output
            SET is_spent = 1
            WHERE
                tx_id = (SELECT ckb_transaction.id FROM ckb_transaction WHERE tx_hash = $1)
                AND output_index = $2
        "#,
    )
    .bind(output_tx_hash)
    .bind(output_index as i32)
    .execute(tx.as_mut())
    .await
    .map_err(|err| Error::DB(err.to_string()))?
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
