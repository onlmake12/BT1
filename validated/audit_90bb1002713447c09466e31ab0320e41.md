The bug is real and clearly demonstrable. Let me trace the full path.

---

### Title
`build_cell_output` Unconditionally Sets `type_(Some(...))` for No-Type-Script Cells, Corrupting Custom-Filter Input Tracking — (`util/rich-indexer/src/indexer/insert.rs`)

### Summary

`build_cell_output` always calls `.type_(Some(type_script))` on the reconstructed `CellOutput`, even when the original on-chain cell had no type script. When a custom cell filter is enabled, this causes `is_cell_filter_match` to incorrectly reject spent no-type-script cells, resulting in missing input records and incomplete transaction history in the indexer's filtered view.

### Finding Description

In `build_cell_output` (lines 621–637), when a cell has no type script, the SQL LEFT JOIN returns NULL for all three type-script columns. All three `Option` variables (`type_code_hash`, `type_hash_type`, `type_args`) are `None`, so `type_builder` remains a `ScriptBuilder::default()`. The built script is a default-zeroed empty `Script`. Then, unconditionally:

```rust
let cell_output = CellOutput::new_builder()
    .capacity(capacity as u64)
    .lock(lock_script)
    .type_(Some(type_script))   // ← always Some, even for no-type-script cells
    .build();
``` [1](#0-0) 

The correct behavior for a no-type-script cell is `.type_(None)`.

This incorrect `CellOutput` is returned by `query_output_cell` and fed directly into `is_cell_filter_match` in `insert_transaction`:

```rust
if self.custom_filters.is_cell_filter_enabled() {
    if let Some((output_id, output, output_data)) =
        query_output_cell(&out_point, tx).await?
        && self.custom_filters.is_cell_filter_match(&output, &output_data.into())
    {
        build_input_rows(output_id, &input, input_index, &mut input_rows);
        is_tx_matched = true;
    }
}
``` [2](#0-1) 

Any custom filter that checks for the absence of a type script (e.g., `output.type_().to_opt().is_none()`) will always evaluate to `false` for every cell retrieved from the DB, because `type_().to_opt()` returns `Some(default_empty_script)` instead of `None`.

### Impact Explanation

When a custom cell filter is enabled and configured to match no-type-script cells:

1. `spend_cell` correctly marks the cell as spent (`is_spent = 1`) — balance queries on unspent outputs remain correct.
2. However, `is_cell_filter_match` on the reconstructed cell always returns `false` for no-type-script cells.
3. `build_input_rows` is never called for such inputs — the `input` table is missing entries.
4. `is_tx_matched` is never set for these inputs — the spending transaction may not be recorded as filter-matched.

The result is **incomplete transaction history** and **missing input records** in the indexer's filtered view. Wallets or applications relying on the indexer's transaction history (e.g., to display sent transactions, compute historical balances, or audit cell provenance) will see corrupted data. The bug affects every no-type-script cell ever spent while a custom filter is active — which is the common case for plain CKB transfer cells.

### Likelihood Explanation

- No special attacker action is required. Any block containing a transaction that spends a no-type-script output triggers the bug.
- No-type-script outputs are the most common cell type on CKB (plain capacity transfers).
- The bug is systematic and affects all such cells whenever `is_cell_filter_enabled()` returns `true`.
- The only precondition is that the node operator has configured a custom cell filter, which is a documented and supported production feature.

### Recommendation

In `build_cell_output`, check whether any type-script column was non-NULL before constructing the `Some(...)` wrapper:

```rust
let type_script_opt = if type_code_hash.is_some() {
    Some(type_builder.build())
} else {
    None
};

let cell_output = CellOutput::new_builder()
    .capacity(capacity as u64)
    .lock(lock_script)
    .type_(type_script_opt)
    .build();
``` [3](#0-2) 

### Proof of Concept

1. Start a node with the rich indexer and a custom cell filter that matches cells where `type_().to_opt().is_none()`.
2. Insert a block containing a transaction with one output that has no type script.
3. Insert a second block containing a transaction that spends that output.
4. Call `query_output_cell` on the spent outpoint inside a DB transaction and assert `result.unwrap().1.type_().to_opt() == None` — this assertion **fails** with the current code, returning `Some(default_empty_script)`.
5. Observe that the `input` table has no row for the spending input, and the spending transaction is not recorded as filter-matched. [4](#0-3)

### Citations

**File:** util/rich-indexer/src/indexer/insert.rs (L597-640)
```rust
fn build_cell_output(row: Option<AnyRow>) -> Option<(i64, CellOutput, Bytes)> {
    let row = row?;
    let id: i64 = row.get("id");
    let capacity: i64 = row.get("capacity");
    let data: Vec<u8> = row.get("data");
    let lock_code_hash: Option<Vec<u8>> = row.get("lock_code_hash");
    let lock_hash_type: Option<i16> = row.get("lock_hash_type");
    let lock_args: Option<Vec<u8>> = row.get("lock_args");
    let type_code_hash: Option<Vec<u8>> = row.get("type_code_hash");
    let type_hash_type: Option<i16> = row.get("type_hash_type");
    let type_args: Option<Vec<u8>> = row.get("type_args");

    let mut lock_builder = ScriptBuilder::default();
    if let Some(lock_code_hash) = lock_code_hash {
        lock_builder = lock_builder.code_hash(to_fixed_array::<32>(&lock_code_hash[0..32]));
    }
    if let Some(lock_args) = lock_args {
        lock_builder = lock_builder.args(lock_args);
    }
    if let Some(lock_hash_type) = lock_hash_type {
        lock_builder = lock_builder.hash_type(Byte::new(lock_hash_type as u8));
    }
    let lock_script = lock_builder.build();

    let mut type_builder = ScriptBuilder::default();
    if let Some(type_code_hash) = type_code_hash {
        type_builder = type_builder.code_hash(to_fixed_array::<32>(&type_code_hash[0..32]));
    }
    if let Some(type_args) = type_args {
        type_builder = type_builder.args(type_args);
    }
    if let Some(type_hash_type) = type_hash_type {
        type_builder = type_builder.hash_type(Byte::new(type_hash_type as u8));
    }
    let type_script = type_builder.build();

    let cell_output = CellOutput::new_builder()
        .capacity(capacity as u64)
        .lock(lock_script)
        .type_(Some(type_script))
        .build();

    Some((id, cell_output, data.into()))
}
```

**File:** util/rich-indexer/src/indexer/mod.rs (L234-243)
```rust
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
```
