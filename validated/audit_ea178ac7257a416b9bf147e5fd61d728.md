The code is present and readable. Let me trace the exact logic.

### Title
`break` on unindexed input silently skips `spend_cell()` for all subsequent inputs, leaving indexed outputs permanently marked `is_spent=0` — (`util/rich-indexer/src/indexer/mod.rs`)

---

### Summary

In `AsyncRichIndexer::insert_transaction`, when a non-cellbase transaction is processed, the input loop calls `spend_cell()` for each input. If `spend_cell()` returns `false` (meaning the output was not found in the DB — a normal occurrence when `cell_filter` is active), the code executes `break`, terminating the entire loop. Any subsequent inputs, including those whose outputs **are** indexed, never have `spend_cell()` called. Those outputs remain permanently at `is_spent=0` in the DB despite being consumed on-chain.

---

### Finding Description

`spend_cell()` issues an `UPDATE output SET is_spent = 1 WHERE ...` and returns `Ok(rows_affected > 0)`. [1](#0-0) 

It returns `Ok(false)` whenever the referenced output is absent from the `output` table — which is the **expected** case for any output filtered out by `cell_filter`.

Back in `insert_transaction`, the caller treats that `false` as a signal to `break` out of the entire input loop: [2](#0-1) 

The correct behavior is `continue` (skip this unindexed input, proceed to the next). Using `break` means that for any transaction whose **first** input references an unindexed output, **all** remaining inputs are silently skipped. Their corresponding outputs in the DB are never updated to `is_spent=1`.

---

### Impact Explanation

Any indexed output that appears at input position ≥ 1 in such a transaction will permanently show `is_spent=0` in the rich-indexer DB. RPC endpoints that query live cells (`get_cells`, `get_transactions`, balance queries) will return these outputs as unspent phantom cells. Wallets and DeFi protocols relying on the indexer will compute incorrect UTXO sets and balances, enabling double-spend confusion.

---

### Likelihood Explanation

The trigger condition is:
1. `cell_filter` is enabled (a supported, documented operator configuration).
2. A valid on-chain transaction has at least two inputs, where the first input's previous output is not indexed (filtered out) and at least one subsequent input's previous output **is** indexed.

Any user who owns an unindexed output and an indexed output can craft exactly such a transaction. No special privileges, no majority hashpower, no leaked keys are required. The block is valid by all consensus rules; the indexer's bug fires automatically during `append`. [3](#0-2) 

---

### Recommendation

Replace `break` with `continue` at line 232 of `util/rich-indexer/src/indexer/mod.rs`:

```rust
if !spend_cell(&out_point, tx).await? {
    continue;   // output not in DB (filtered); skip, do not abort the loop
}
```

This preserves the existing intent (skip unindexed outputs) while ensuring every subsequent input is still processed.

---

### Proof of Concept

1. Start a CKB node with the rich-indexer and a `cell_filter` that excludes outputs with lock script `LOCK_A` but includes outputs with lock script `LOCK_B`.
2. Mine a block containing:
   - `tx_setup`: cellbase + a tx that creates `output_A` (lock=`LOCK_A`, unindexed) and `output_B` (lock=`LOCK_B`, indexed).
3. Mine a second block containing:
   - `tx_spend`: inputs = [`output_A`, `output_B`], i.e., the unindexed output is input 0.
4. After the indexer processes the second block, query the DB:
   ```sql
   SELECT is_spent FROM output WHERE ... -- output_B's row
   ```
5. **Expected (correct):** `is_spent = 1`.  
   **Actual (buggy):** `is_spent = 0` — `output_B` appears as a live cell despite being consumed on-chain. [4](#0-3)

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

**File:** util/rich-indexer/src/indexer/mod.rs (L156-178)
```rust
    pub(crate) async fn append(&self, block: &BlockView) -> Result<(), Error> {
        let mut tx = self
            .store
            .transaction()
            .await
            .map_err(|err| Error::DB(err.to_string()))?;
        if self.custom_filters.is_block_filter_match(block) {
            let block_id = append_block(block, &mut tx).await?;
            self.insert_transactions(block_id, block, &mut tx).await?;
        } else {
            let block_headers = vec![(block.hash().raw_data().to_vec(), block.number() as i64)];
            bulk_insert_blocks_simple(block_headers, &mut tx).await?;
        }
        tx.commit()
            .await
            .map_err(|err| Error::DB(err.to_string()))?;

        if let Some(mut pool) = self.pool.as_ref().map(|p| p.write().expect("acquire lock")) {
            pool.transactions_committed(&block.transactions());
        }

        Ok(())
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
