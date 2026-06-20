The code is clear. Let me trace the exact execution path.

**The `break` in `insert_transaction`** (`mod.rs` lines 228–248):

```rust
if tx_index != 0 {
    for (input_index, input) in tx_view.inputs().into_iter().enumerate() {
        let out_point = input.previous_output();
        if !spend_cell(&out_point, tx).await? {
            break;          // ← exits the entire loop
        }
        // ... build_input_rows, is_cell_filter_match ...
    }
}
```

**`spend_cell`** (`insert.rs` lines 403–427) issues `UPDATE output SET is_spent = 1 WHERE tx_id = ... AND output_index = ...` and returns `rows_affected > 0`. It returns `false` when the output row does not exist in the DB at all.

**With `cell_filter` enabled**, only outputs that pass `is_cell_filter_match` are inserted into the `output` table. A single transaction can produce a mix: some outputs indexed, some not. `spend_cell` returning `false` for one input therefore does **not** imply all inputs reference unindexed outputs.

**Concrete exploit path:**

1. Node operator enables `cell_filter` (e.g., `output.type != ()`).
2. Block A: Tx T1 creates `output[0]` (typed → passes filter → **indexed**) and `output[1]` (no type script → filtered out → **not indexed**).
3. Block B: Tx T2 spends both, with inputs ordered `[T1:output[1], T1:output[0]]`. Any transaction creator controls input ordering.
4. Indexer processes T2:
   - `spend_cell(T1:output[1])` → 0 rows updated → returns `false` → **`break`**
   - `spend_cell(T1:output[0])` is **never called**
   - `T1:output[0]` remains `is_spent = 0` in the DB permanently.
5. All subsequent `get_cells` / `get_cells_capacity` queries return `T1:output[0]` as a live cell.

**Secondary compounding bug** in `build_cell_output` (`insert.rs` lines 633–637): when `query_output_cell` reconstructs a stored cell that had no type script (NULL `type_script_id`), it always calls `.type_(Some(type_script))` with a default-built (empty) `ScriptBuilder`, producing a non-`None` type script. This means `is_cell_filter_match` re-evaluated on the reconstructed cell may produce a different result than on the original cell — but this is secondary to the `break` bug.

**Root cause**: `break` should be `continue` when `cell_filter` is enabled. The `break` was presumably written under the assumption that if one output is absent from the DB, the whole creating transaction was unindexed — an assumption that is false under partial cell-filter indexing.

---

### Title
`insert_transaction` breaks input loop on first unindexed cell, leaving co-transaction indexed outputs permanently unspent — (`util/rich-indexer/src/indexer/mod.rs`)

### Summary
When `cell_filter` is active, `insert_transaction` iterates inputs and calls `spend_cell` for each. If `spend_cell` returns `false` (output not in DB), the loop `break`s. Because a single transaction can produce both filter-matching (indexed) and non-matching (unindexed) outputs, an adversary who controls input ordering can place an unindexed output's input before an indexed output's input, causing the indexed output to never be marked spent.

### Finding Description
In `util/rich-indexer/src/indexer/mod.rs` lines 228–248, the input-processing loop unconditionally `break`s when `spend_cell` returns `false`: [1](#0-0) 

`spend_cell` in `util/rich-indexer/src/indexer/insert.rs` returns `false` when the `UPDATE` touches zero rows — i.e., the output row is absent from the DB: [2](#0-1) 

Outputs are only inserted when `is_cell_filter_match` returns `true`: [3](#0-2) 

So a transaction with mixed outputs (some indexed, some not) leaves the loop vulnerable to premature termination. The `break` should be `continue` under `cell_filter`.

A secondary bug in `build_cell_output` always reconstructs a non-`None` type script even for cells stored without one: [4](#0-3) 

### Impact Explanation
The rich-indexer permanently retains `is_spent = 0` for any indexed output whose spending transaction also spends an unindexed output that appears earlier in the input list. All `get_cells` and `get_cells_capacity` queries will return these cells as live, corrupting the live-cell set for any application relying on the indexer.

### Likelihood Explanation
Any unprivileged user who creates a valid transaction spending both a filter-matching and a non-filter-matching output from the same prior transaction can trigger this. Input ordering is freely chosen by the transaction creator. No special privileges, hashpower, or key material are required.

### Recommendation
Replace `break` with `continue` in the input loop when `cell_filter` is enabled, so that inputs referencing unindexed outputs are silently skipped rather than aborting the entire loop:

```rust
if !spend_cell(&out_point, tx).await? {
    if self.custom_filters.is_cell_filter_enabled() {
        continue;   // unindexed output — skip, but keep processing remaining inputs
    } else {
        break;
    }
}
```

Also fix `build_cell_output` to return `None` for the type script when `type_script_id` is NULL, so that `is_cell_filter_match` re-evaluation on stored cells is consistent with the original evaluation.

### Proof of Concept
1. Start a CKB node with rich-indexer and `cell_filter = "output.type != ()"`.
2. Submit Block A containing Tx T1 with two outputs: `output[0]` has a type script (indexed), `output[1]` has no type script (not indexed).
3. Submit Block B containing Tx T2 with inputs `[{previous_output: T1:1}, {previous_output: T1:0}]` (unindexed input first).
4. Query `get_cells` for the lock script of `T1:output[0]`.
5. **Expected**: no results (cell is spent). **Actual**: `T1:output[0]` is returned as a live cell with `is_spent = 0`.

### Citations

**File:** util/rich-indexer/src/indexer/mod.rs (L217-226)
```rust
        for (output_index, (cell, data)) in tx_view.outputs_with_data_iter().enumerate() {
            if self
                .custom_filters
                .is_cell_filter_match(&cell, &(&data).into())
            {
                build_output_cell_rows(&cell, output_index, &data, &mut output_cell_rows);
                build_script_set(&cell, &mut script_set).await;
                is_tx_matched = true;
            }
        }
```

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

**File:** util/rich-indexer/src/indexer/insert.rs (L633-637)
```rust
    let cell_output = CellOutput::new_builder()
        .capacity(capacity as u64)
        .lock(lock_script)
        .type_(Some(type_script))
        .build();
```
