I have found the exact analog. Let me confirm the precise line numbers and the `spend_cell` return semantics.

### Title
Rich-Indexer `insert_transaction` Breaks Input Loop on First Untracked Cell, Leaving Subsequent Spent Cells Marked as Live — (`File: util/rich-indexer/src/indexer/mod.rs`)

---

### Summary

In `util/rich-indexer/src/indexer/mod.rs`, the `insert_transaction` function iterates over a transaction's inputs to mark each consumed cell as spent via `spend_cell(...)`. When `spend_cell` returns `false` (meaning the referenced output is not present in the rich-indexer's database — a normal condition for cells created before indexing began or outside the custom filter), the loop executes `break` instead of `continue`. This halts all further input processing for that transaction. Every input after the first untracked one is silently skipped: those cells are never marked `is_spent = 1`, so they remain permanently visible as live cells to any RPC caller querying `get_cells` or `get_cells_capacity`.

---

### Finding Description

`insert_transaction` in `util/rich-indexer/src/indexer/mod.rs` processes each non-cellbase transaction input:

```rust
if tx_index != 0 {
    for (input_index, input) in tx_view.inputs().into_iter().enumerate() {
        let out_point = input.previous_output();
        if !spend_cell(&out_point, tx).await? {
            break;          // ← BUG: should be `continue`
        }
        // ... build input rows, mark tx_matched
    }
}
``` [1](#0-0) 

`spend_cell` issues a SQL `UPDATE output SET is_spent = 1 WHERE tx_id = ... AND output_index = ...` and returns `Ok(true)` if a row was updated, `Ok(false)` if zero rows were affected (i.e., the cell is simply not in the database):

```rust
Ok(updated_rows > 0)
``` [2](#0-1) 

Returning `false` is a routine, non-error outcome: it happens whenever an input's previous output was created before the indexer's start height, or does not match the configured custom filter. The correct response is `continue` — skip this input and process the next. Instead, `break` exits the entire loop, so every input after the first untracked one is never processed. Those cells are never updated to `is_spent = 1`.

---

### Impact Explanation

`get_cells` and `get_cells_capacity` both filter on `output.is_spent = 0` to enumerate live cells:

```rust
.and_where("output.is_spent = 0"); // live cells
``` [3](#0-2) [4](#0-3) 

Any cell whose `is_spent` flag was never set to `1` due to the premature `break` will be returned as a live cell by these RPC endpoints indefinitely. This corrupts the indexer's view of the UTXO set: spent cells appear unspent, capacity totals are inflated, and any application relying on the rich-indexer for balance or UTXO queries receives incorrect data. The database state diverges from the actual chain state and cannot self-correct without a full re-index.

---

### Likelihood Explanation

The trigger condition is common: a transaction with two or more inputs where the **first** input's previous output is absent from the rich-indexer's database. This occurs naturally whenever:

- The indexer was started at a block height after the first input's cell was created (partial sync / late-start indexing).
- A custom cell filter is active and the first input's cell does not match it, while a later input's cell does.

Any unprivileged transaction sender can craft or naturally produce such a transaction. No special privileges, keys, or majority hash power are required.

---

### Recommendation

Replace `break` with `continue` on line 232 of `util/rich-indexer/src/indexer/mod.rs`:

```rust
if !spend_cell(&out_point, tx).await? {
    continue;   // cell not tracked; skip this input, process the rest
}
```

This matches the semantics of `spend_cell` returning `false` as a normal "not found" signal rather than a fatal error, and ensures every input in a transaction is independently evaluated.

---

### Proof of Concept

Consider a confirmed block containing transaction `T` with two inputs:

- **Input 0**: spends cell `C0`, which was created before the indexer's start height → `spend_cell(C0)` returns `Ok(false)` → `break` fires → loop exits.
- **Input 1**: spends cell `C1`, which is fully tracked by the indexer → **never reached**.

After block processing, `C1.is_spent` remains `0`. A subsequent call to `get_cells` with `C1`'s lock script returns `C1` as a live cell, even though it has been spent on-chain. An RPC caller (e.g., a wallet or DApp) computing a user's balance will double-count `C1`'s capacity. [5](#0-4) [6](#0-5)

### Citations

**File:** util/rich-indexer/src/indexer/mod.rs (L228-249)
```rust
        if tx_index != 0 {
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
        }
```

**File:** util/rich-indexer/src/indexer/insert.rs (L403-427)
```rust
pub(crate) async fn spend_cell(
    out_point: &OutPoint,
    tx: &mut Transaction<'_, Any>,
) -> Result<bool, Error> {
    let output_tx_hash = out_point.tx_hash().raw_data().to_vec();
    let output_index: u32 = out_point.index().into();

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
}
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L107-107)
```rust
        .and_where("output.is_spent = 0"); // live cells
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L71-71)
```rust
        query_builder.and_where("output.is_spent = 0"); // live cells
```
