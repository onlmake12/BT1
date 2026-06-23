### Title
`prune()` Skips Header and TxHash Pruning When No ConsumedOutPoint Entries Exist — (`util/indexer/src/indexer.rs`)

### Summary

The `prune()` function in `Indexer` uses `min_block_number` — derived exclusively from `ConsumedOutPoint` entries — as the seek key for the `Header` and `TxHash` pruning iterator. When no `ConsumedOutPoint` entries exist (i.e., all pruneable blocks contain only cellbase transactions), `min_block_number` stays at `BlockNumber::MAX` (`u64::MAX`), causing the iterator to seek to the end of the `Header` key space and find nothing. As a result, `TxHash => TransactionInputs` and `Header => Transactions` entries for old blocks are never deleted, growing without bound.

---

### Finding Description

In `prune()`: [1](#0-0) 

`min_block_number` is initialized to `BlockNumber::MAX` at line 765. The loop at lines 766–781 only sets it to a real block number if at least one `ConsumedOutPoint` entry with `block_number < prune_to_block` exists. If the loop body never executes (no such entries), `min_block_number` remains `u64::MAX`.

At line 785, `key_prefix_header` is extended with `min_block_number.to_be_bytes()` = `[0xFF; 8]`. The resulting iterator seek key is `[224u8, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]` — the very end of the `Header` key space. The `take_while` guard at line 793 (`BlockNumber <= prune_to_block`) immediately terminates because no real blocks exist at block `u64::MAX`. Neither `Header` nor `TxHash` entries are deleted. [2](#0-1) 

**Why `ConsumedOutPoint` entries are absent for cellbase-only blocks:**

In `append()`, the input-processing loop is gated on `tx_index > 0` (line 339), so cellbase transactions (always `tx_index == 0`) never produce `ConsumedOutPoint` entries: [3](#0-2) 

However, cellbase outputs ARE processed (line 424 onward), and if any output matches the cell filter, `tx_matched = true`, causing a `TxHash` entry to be written (lines 482–491) and a `Header` entry to be written (lines 494–510): [4](#0-3) 

So for a chain of cellbase-only blocks: `TxHash` and `Header` entries accumulate every block, but `prune()` never removes any of them.

---

### Impact Explanation

- `TxHash => TransactionInputs` entries (prefix `192`) and `Header => Transactions` entries (prefix `224`) grow proportionally to chain length with no upper bound.
- The indexer's RocksDB store is never compacted of these entries, degrading read/scan performance and consuming unbounded disk space.
- The existing `prune_bound` test (lines 1532–1600) exercises exactly this scenario (21 cellbase-only blocks) but only asserts that `get_block_hash` works — it does **not** assert that old `TxHash`/`Header` entries are absent after pruning, so the bug is untested. [5](#0-4) 

---

### Likelihood Explanation

This is triggered by any chain segment where non-cellbase transactions are absent — a normal condition during low-activity periods on mainnet, not requiring any attacker. Any miner producing valid cellbase-only blocks (standard behavior) triggers this path. The condition is reachable without any special privilege.

---

### Recommendation

Decouple the `Header`/`TxHash` pruning start key from `min_block_number`. When `min_block_number == BlockNumber::MAX` (no `ConsumedOutPoint` entries were pruned), the Header/TxHash iterator should still start from block `0` (or the earliest stored header), not from `u64::MAX`. A minimal fix:

```rust
let start_block = if min_block_number == BlockNumber::MAX {
    0u64
} else {
    min_block_number
};
let mut key_prefix_header = vec![KeyPrefix::Header as u8];
key_prefix_header.extend_from_slice(&start_block.to_be_bytes());
```

---

### Proof of Concept

1. Create an `Indexer` with `keep_num = 10`, `prune_interval = 1`.
2. Append `keep_num + 2 = 12` blocks, each containing only a cellbase transaction with one output (no non-cellbase txs).
3. Call `prune()` (it is also called automatically on each `append` since `prune_interval = 1`).
4. Scan all keys with prefix `KeyPrefix::TxHash` (192) and `KeyPrefix::Header` (224).
5. Assert that entries for blocks `<= tip - keep_num - 1` are absent — they will **not** be absent, demonstrating the bug.

The `prune_bound` test at line 1534 already sets up this exact scenario but does not assert the pruning invariant, confirming the gap. [6](#0-5)

### Citations

**File:** util/indexer/src/indexer.rs (L338-421)
```rust
            // skip cellbase
            if tx_index > 0 {
                for (input_index, input) in tx.inputs().into_iter().enumerate() {
                    // delete live cells related kv and mark it as consumed (for rollback and forking)
                    // insert lock / type => tx_hash mapping
                    let input_index = input_index as u32;
                    let out_point = input.previous_output();
                    let key_vec = Key::OutPoint(&out_point).into_vec();

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

**File:** util/indexer/src/indexer.rs (L479-510)
```rust
            if tx_matched {
                matched_txs.push((tx.hash(), tx.outputs().len() as u32, Some(tx_index)));
                // insert tx
                batch.put_kv(
                    Key::TxHash(&tx_hash),
                    Value::TransactionInputs(
                        tx.inputs()
                            .into_iter()
                            .map(|input| input.previous_output())
                            .collect(),
                    ),
                )?;
            }
        }

        // insert block transactions
        if matched_txs.len() == transactions.len() {
            batch.put_kv(
                Key::Header(block.number(), &block.hash(), false),
                Value::Transactions(
                    matched_txs
                        .into_iter()
                        .map(|(tx_hash, outputs_len, _)| (tx_hash, outputs_len, None))
                        .collect(),
                ),
            )?;
        } else {
            batch.put_kv(
                Key::Header(block.number(), &block.hash(), true),
                Value::Transactions(matched_txs),
            )?;
        }
```

**File:** util/indexer/src/indexer.rs (L753-810)
```rust
    pub(crate) fn prune(&self) -> Result<(), Error> {
        let (tip_number, _tip_hash) = self.tip()?.expect("stored tip");
        let prune_number = self.keep_num + 1;
        if tip_number > prune_number {
            let prune_to_block = tip_number - prune_number;
            let mut batch = self.store.batch()?;
            // prune ConsumedOutPoint => Cell
            let key_prefix_consumed_out_point = vec![KeyPrefix::ConsumedOutPoint as u8];
            let iter = self
                .store
                .iter(&key_prefix_consumed_out_point, IteratorDirection::Forward)?
                .take_while(|(key, _value)| key.starts_with(&key_prefix_consumed_out_point));
            let mut min_block_number = BlockNumber::MAX;
            for (block_number, key) in iter
                .map(|(key, _value)| {
                    (
                        BlockNumber::from_be_bytes(
                            key[1..9].try_into().expect("stored block_number"),
                        ),
                        key,
                    )
                })
                .take_while(|(block_number, _key)| prune_to_block.gt(block_number))
            {
                if min_block_number == BlockNumber::MAX {
                    min_block_number = block_number;
                }
                batch.delete(key)?;
            }

            // prune Header => Transactions and TxHash => TransactionInputs
            let mut key_prefix_header = vec![KeyPrefix::Header as u8];
            key_prefix_header.extend_from_slice(&min_block_number.to_be_bytes());
            let iter = self
                .store
                .iter(&key_prefix_header, IteratorDirection::Forward)?
                .take_while(|(key, _value)| {
                    key.starts_with(&[KeyPrefix::Header as u8])
                        && BlockNumber::from_be_bytes(
                            key[1..9].try_into().expect("stored block_number"),
                        ) <= prune_to_block
                });
            for (txs, header_key) in iter.map(|(header_key, value)| {
                (
                    Value::parse_transactions_value(&value, header_key.len() == 42),
                    header_key,
                )
            }) {
                for (tx_hash, _outputs_len, _tx_index) in txs {
                    batch.delete(Key::TxHash(&tx_hash).into_vec())?;
                }
                batch.delete(header_key)?;
            }

            batch.commit()?;
        }
        Ok(())
    }
```

**File:** util/indexer/src/indexer.rs (L1532-1600)
```rust
    // This case is to test whether the prune boundary affects the rollback history block
    #[test]
    fn prune_bound() {
        let indexer = new_indexer::<RocksdbStore>("prune");

        let lock_script1 = ScriptBuilder::default()
            .code_hash(H256(rand::random()))
            .hash_type(ScriptHashType::Data)
            .args(Bytes::from(b"lock_script1".to_vec()))
            .build();

        let cellbase0 = TransactionBuilder::default()
            .input(CellInput::new_cellbase_input(0))
            .witness(Script::default().into_witness())
            .output(
                CellOutputBuilder::default()
                    .capacity(capacity_bytes!(1000))
                    .lock(lock_script1.clone())
                    .build(),
            )
            .output_data(Bytes::default())
            .build();

        let block0 = BlockBuilder::default()
            .transaction(cellbase0)
            .header(HeaderBuilder::default().number(0).build())
            .build();

        indexer.append(&block0).unwrap();

        let mut pre_block = block0;

        for i in 0..20 {
            let cellbase = TransactionBuilder::default()
                .input(CellInput::new_cellbase_input(i + 1))
                .witness(Script::default().into_witness())
                .output(
                    CellOutputBuilder::default()
                        .capacity(capacity_bytes!(1000))
                        .lock(lock_script1.clone())
                        .build(),
                )
                .output_data(Bytes::default())
                .build();

            pre_block = BlockBuilder::default()
                .transaction(cellbase)
                .header(
                    HeaderBuilder::default()
                        .number(pre_block.number() + 1)
                        .parent_hash(pre_block.hash())
                        .epoch(EpochNumberWithFraction::new(
                            pre_block.number() + 1,
                            pre_block.number(),
                            1000,
                        ))
                        .build(),
                )
                .build();

            indexer.append(&pre_block).unwrap();
        }

        let (tip_number, _) = indexer.tip().unwrap().unwrap();
        let longest_fork_number = tip_number.saturating_sub(KEEP_NUM);
        let rollback_start = indexer.get_block_hash(longest_fork_number);
        assert!(rollback_start.is_ok());
        assert!(rollback_start.unwrap().is_some());
    }
```
