### Title
`break` Instead of `continue` in Input Loop Causes Incomplete Cell Spending in Rich-Indexer — (`File: util/rich-indexer/src/indexer/mod.rs`)

---

### Summary

In `AsyncRichIndexer::insert_transaction`, the loop that iterates over a transaction's inputs to mark each consumed cell as spent uses `break` when `spend_cell` returns `false`. This terminates the entire loop on the first untracked input, silently skipping all subsequent inputs. As a result, cells that should be marked `is_spent = 1` in the rich-indexer's SQL database remain as live cells, corrupting the off-chain index state.

---

### Finding Description

`insert_transaction` in `util/rich-indexer/src/indexer/mod.rs` processes each input of a confirmed transaction by calling `spend_cell`, which issues an SQL `UPDATE output SET is_spent = 1 WHERE ...` and returns `Ok(true)` if a row was updated, `Ok(false)` if no row matched (i.e., the cell is not present in the `output` table).

The loop at lines 229–248:

```rust
for (input_index, input) in tx_view.inputs().into_iter().enumerate() {
    let out_point = input.previous_output();
    if !spend_cell(&out_point, tx).await? {
        break;          // ← terminates the entire loop
    }
    // ... build_input_rows ...
}
```

When `spend_cell` returns `false` for input `i`, the `break` exits the loop entirely. Inputs `i+1`, `i+2`, … are never visited. Their corresponding cells are never marked spent.

`spend_cell` returns `false` whenever the referenced output is absent from the `output` table — a normal condition when:
- The cell was produced by a transaction filtered out by `custom_filters` (cell filter or block filter).
- The cell originates from a genesis transaction that was not indexed.
- The indexer was initialized with `set_init_tip` at a height after the cell was created. [1](#0-0) 

`spend_cell` itself: [2](#0-1) 

---

### Impact Explanation

**Severity: Medium**

The rich-indexer's SQL `output` table retains stale `is_spent = 0` rows for cells that have actually been consumed on-chain. Any RPC caller using the rich-indexer's `get_cells` or `get_transactions` endpoints will receive incorrect results: already-spent cells appear live. This corrupts the off-chain view of cell liveness for all scripts whose inputs happen to follow an untracked input in the same transaction.

The corruption is permanent until the indexer is re-synced from scratch; rollback does not repair it because `reset_spent_cells` operates on the `tx_id_list` of the rolled-back block, not on the cells that were silently skipped during append. [3](#0-2) 

---

### Likelihood Explanation

**Likelihood: High**

Any transaction that spends at least two cells where the **first** input's cell is absent from the `output` table triggers the bug. This is routine when:

1. **Custom cell filters are active** — a common deployment configuration. A transaction may spend one unfiltered cell (not in the DB) followed by one filtered cell (should be marked spent). The `break` prevents the second cell from ever being updated.
2. **Indexer initialized mid-chain** (`set_init_tip`) — cells created before the init tip are absent from the DB; any transaction spending such a cell as its first input silently skips all remaining inputs.

No special attacker capability is required. Any unprivileged transaction sender who submits a multi-input transaction triggers this path.

---

### Recommendation

Replace `break` with `continue` so that the loop skips inputs whose cells are not present in the database but continues processing all remaining inputs:

```rust
if !spend_cell(&out_point, tx).await? {
    continue;   // cell not in DB, skip but keep processing remaining inputs
}
``` [4](#0-3) 

---

### Proof of Concept

Consider a block containing transaction `T` with two inputs:

- **Input 0**: spends cell `A`, produced by a transaction excluded by the cell filter → not in `output` table → `spend_cell(A)` returns `false` → **`break`** fires.
- **Input 1**: spends cell `B`, produced by a tracked transaction → **never reached**.

After `insert_transaction` returns, cell `B` still has `is_spent = 0` in the database. A subsequent `get_cells` query for the script locking cell `B` returns it as a live cell, even though it has been consumed on-chain.

The root cause is the single `break` statement at line 232 of `util/rich-indexer/src/indexer/mod.rs`, which should be `continue`. [5](#0-4)

### Citations

**File:** util/rich-indexer/src/indexer/mod.rs (L228-248)
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
```

**File:** util/rich-indexer/src/indexer/insert.rs (L403-426)
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
```

**File:** util/rich-indexer/src/indexer/remove.rs (L17-18)
```rust
    // update spent cells
    reset_spent_cells(&tx_id_list, tx).await?;
```
