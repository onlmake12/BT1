The code is confirmed. Let me verify the exact semantics of `spend_cell` and whether the `break` is intentional or a bug.

### Title
`break` on Unindexed Input Silently Skips Remaining Inputs, Leaving Indexed Cells Permanently Unspent — (`util/rich-indexer/src/indexer/mod.rs`)

---

### Summary

`AsyncRichIndexer::insert_transaction` uses `break` when `spend_cell` returns `Ok(false)` for an input whose out-point is absent from the indexer DB. This exits the entire input loop, so all subsequent inputs in the same transaction are never processed. Any indexed live cells referenced by those later inputs remain with `is_spent = 0` permanently, causing the indexer to diverge from chain state.

---

### Finding Description

In `insert_transaction`, the input-processing loop is:

```rust
if tx_index != 0 {
    for (input_index, input) in tx_view.inputs().into_iter().enumerate() {
        let out_point = input.previous_output();
        if !spend_cell(&out_point, tx).await? {
            break;          // ← exits the entire loop
        }
        // build input_rows, mark is_tx_matched …
    }
}
``` [1](#0-0) 

`spend_cell` issues an `UPDATE output SET is_spent = 1 WHERE …` and returns `Ok(rows_affected > 0)`. [2](#0-1) 

It returns `Ok(false)` — zero rows affected — whenever the referenced out-point is **not present** in the `output` table. This is the normal, expected outcome for:

- Cells filtered out by a configured `cell_filter` (Rhai script). [3](#0-2) 

- Cells from blocks before the configured `init_tip_hash` (never indexed). [4](#0-3) 

- Genesis cells that were never inserted into the `output` table.

When `spend_cell` returns `Ok(false)` for `input[0]`, the `break` terminates the loop. `input[1..N]` are never visited, so `spend_cell` is never called for them, and their rows in the `output` table keep `is_spent = 0`. The DB transaction is then committed with those cells still marked live. [5](#0-4) 

The correct fix is `continue` (skip the unindexed input, process the rest), not `break`.

---

### Impact Explanation

After the consuming block is committed, any indexed cell referenced by `input[1..N]` is permanently visible as a live cell via `get_cells` / `get_cells_capacity` RPCs. The inconsistency survives node restarts because it is persisted in SQLite/PostgreSQL. Downstream applications (wallets, DEXes, bridges) that rely on the indexer for live-cell queries will treat already-spent cells as spendable, leading to:

- Construction of invalid transactions that reference dead cells (rejected by consensus but built on bad indexer data).
- Incorrect capacity accounting in `get_cells_capacity`.
- Stale transaction history in `get_transactions`.

---

### Likelihood Explanation

The trigger requires only a **valid, consensus-accepted transaction** whose inputs are ordered so that `input[0]` references a cell absent from the indexer DB. This is trivially achievable by any user who:

1. Holds a cell that is excluded by the operator's `cell_filter` (a documented, common configuration — e.g., index only TYPE-ID cells), **and**
2. Also holds at least one indexed live cell.

They simply place the filtered cell first in the input list. No miner collusion, no privileged access, no key leakage is required. The CKB protocol imposes no ordering constraint on transaction inputs, so the ordering is fully attacker-controlled.

---

### Recommendation

Replace `break` with `continue` at line 232:

```rust
if !spend_cell(&out_point, tx).await? {
    continue;   // cell not in indexer DB (filtered/pre-init-tip); skip, don't abort loop
}
``` [6](#0-5) 

This matches the semantics of the legacy RocksDB indexer, which uses `if let Some(stored_live_cell) = … { … }` (an implicit `continue` on miss) rather than aborting the loop. [7](#0-6) 

---

### Proof of Concept

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

### Citations

**File:** util/rich-indexer/src/indexer/mod.rs (L169-171)
```rust
        tx.commit()
            .await
            .map_err(|err| Error::DB(err.to_string()))?;
```

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

**File:** util/indexer-sync/src/custom_filters.rs (L97-115)
```rust
    /// Returns true if the cell filter is match
    pub fn is_cell_filter_match(&self, output: &CellOutput, output_data: &Bytes) -> bool {
        self.cell_filter
            .as_ref()
            .map(|cell_filter| {
                let json_output: ckb_jsonrpc_types::CellOutput = output.clone().into();
                let parsed_output = self
                    .engine
                    .parse_json(serde_json::to_string(&json_output).unwrap(), true)
                    .unwrap();
                let mut scope = Scope::new();
                scope.push("output", parsed_output);
                scope.push("output_data", format!("{output_data:#x}"));
                self.engine
                    .eval_ast_with_scope(&mut scope, cell_filter)
                    .expect("eval cell_filter should be ok")
            })
            .unwrap_or(true)
    }
```

**File:** util/app-config/src/configs/indexer.rs (L35-37)
```rust
    /// The init tip block hash
    #[serde(default)]
    pub init_tip_hash: Option<H256>,
```

**File:** util/indexer/src/indexer.rs (L347-421)
```rust
                    if let Some(stored_live_cell) = self.store.get(&key_vec)?.or_else(|| {
                        transactions
                            .iter()
                            .enumerate()
                            .find(|(_i, tx)| tx.hash() == out_point.tx_hash())
                            .map(|(i, tx)| {
                                let idx = out_point.index().into();
                                Value::Cell(
                                    block_number,
                                    i as u32,
                                    &tx.outputs().get(idx).expect("index should match"),
                                    &tx.outputs_data().get(idx).expect("index should match"),
                                )
                                .into()
                            })
                    }) {
                        let (generated_by_block_number, generated_by_tx_index, output, output_data) =
                            Value::parse_cell_value(&stored_live_cell);

                        if !self
                            .custom_filters
                            .is_cell_filter_match(&output, &output_data)
                        {
                            continue;
                        } else {
                            tx_matched = true;
                        }

                        batch.delete(
                            Key::CellLockScript(
                                &output.lock(),
                                generated_by_block_number,
                                generated_by_tx_index,
                                out_point.index().into(),
                            )
                            .into_vec(),
                        )?;
                        batch.put_kv(
                            Key::TxLockScript(
                                &output.lock(),
                                block_number,
                                tx_index,
                                input_index,
                                CellType::Input,
                            ),
                            Value::TxHash(&tx_hash),
                        )?;
                        if let Some(script) = output.type_().to_opt() {
                            batch.delete(
                                Key::CellTypeScript(
                                    &script,
                                    generated_by_block_number,
                                    generated_by_tx_index,
                                    out_point.index().into(),
                                )
                                .into_vec(),
                            )?;
                            batch.put_kv(
                                Key::TxTypeScript(
                                    &script,
                                    block_number,
                                    tx_index,
                                    input_index,
                                    CellType::Input,
                                ),
                                Value::TxHash(&tx_hash),
                            )?;
                        };
                        batch.delete(key_vec)?;
                        batch.put_kv(
                            Key::ConsumedOutPoint(block_number, &out_point),
                            stored_live_cell,
                        )?;
                    }
                }
```
