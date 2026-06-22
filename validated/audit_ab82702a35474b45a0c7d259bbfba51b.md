### Title
Indexer `prune()` Leaves `TxLockScript` and `TxTypeScript` Index Entries in Storage — (`File: util/indexer/src/indexer.rs`)

---

### Summary

The `prune()` function in the CKB indexer deletes `ConsumedOutPoint` and `TxHash` entries for old blocks but omits deletion of the corresponding `TxLockScript` (prefix 128) and `TxTypeScript` (prefix 160) index entries. These entries accumulate indefinitely in storage and remain queryable via the `get_transactions` RPC, returning stale index references that point to already-pruned data.

---

### Finding Description

The indexer storage schema defines eight key-prefix families:

```
| 0   | OutPoint           | Cell                     |
| 32  | ConsumedOutPoint   | Cell                     | * rollback and prune
| 64  | CellLockScript     | TxHash                   |
| 96  | CellTypeScript     | TxHash                   |
| 128 | TxLockScript       | TxHash                   |
| 160 | TxTypeScript       | TxHash                   |
| 192 | TxHash             | TransactionInputs        | * rollback and prune
| 224 | Header             | Transactions             |
```

The schema comment `* rollback and prune` marks only `ConsumedOutPoint` (32) and `TxHash` (192) as pruning targets. The `TxLockScript` (128) and `TxTypeScript` (160) families are not marked.

During `append()`, when a cell is spent, the indexer inserts:
- `TxLockScript(script, block_number, tx_index, input_index, Input)` → `TxHash`
- `TxTypeScript(script, block_number, tx_index, input_index, Input)` → `TxHash` (if type script present)

And for each output:
- `TxLockScript(script, block_number, tx_index, output_index, Output)` → `TxHash`
- `TxTypeScript(script, block_number, tx_index, output_index, Output)` → `TxHash` (if type script present) [1](#0-0) 

The `prune()` function only deletes `ConsumedOutPoint` keys and `TxHash` keys for blocks older than `tip - keep_num`, then deletes the `Header` entries: [2](#0-1) 

There is no deletion of `TxLockScript` or `TxTypeScript` entries anywhere in `prune()`. These entries encode the script bytes directly in the key and accumulate without bound.

---

### Impact Explanation

1. **Unbounded storage growth**: Every spent cell and every output in every pruned block leaves behind at least one `TxLockScript` entry and optionally one `TxTypeScript` entry. These are never reclaimed, causing the indexer RocksDB store to grow indefinitely regardless of the configured `keep_num`.

2. **Stale/incorrect `get_transactions` RPC results**: The `get_transactions` RPC iterates over `TxLockScript` / `TxTypeScript` entries to find transactions by script. After pruning, these entries still exist but their corresponding `TxHash` (transaction inputs) entries have been deleted. An RPC caller querying transactions for a script will encounter stale index entries referencing pruned data, producing incorrect or incomplete results. [3](#0-2) [4](#0-3) 

---

### Likelihood Explanation

- `prune()` is called automatically every `prune_interval` blocks during normal `append()` operation.
- Any node running the indexer (the default configuration for full nodes serving RPC) is affected.
- The accumulation is proportional to chain activity: a busy chain with many transactions and type scripts will exhaust disk space faster.
- Any unprivileged RPC caller can trigger `get_transactions` and observe the stale results. [5](#0-4) 

---

### Recommendation

In `prune()`, after collecting the set of `(block_number, tx_hash, outputs_len)` tuples from the `Header` entries being pruned, reconstruct and delete the corresponding `TxLockScript` and `TxTypeScript` keys. This requires reading the cell output data (lock/type scripts) from the `ConsumedOutPoint` values before deleting them, or storing a separate index of which script keys exist per block. The deletion of `TxLockScript`/`TxTypeScript` entries should be added alongside the existing `ConsumedOutPoint` and `TxHash` deletions in the prune batch. [6](#0-5) 

---

### Proof of Concept

1. Start a CKB node with the indexer enabled and `keep_num = 10`.
2. Submit transactions that spend cells with lock scripts and type scripts across 30+ blocks.
3. After block 21, `prune()` fires and deletes `ConsumedOutPoint` and `TxHash` entries for blocks 0–10.
4. Query the RocksDB store directly: `TxLockScript` and `TxTypeScript` entries for blocks 0–10 still exist.
5. Call `get_transactions` RPC with the lock script used in block 0. The RPC iterates the stale `TxLockScript` entries and returns references to transactions whose `TxHash` (inputs) data has been pruned, producing incorrect results.
6. Repeat for 10,000 blocks: the `TxLockScript`/`TxTypeScript` prefix in RocksDB grows without bound while `ConsumedOutPoint` stays bounded. [7](#0-6) [8](#0-7)

### Citations

**File:** util/indexer/src/indexer.rs (L31-41)
```rust
/// | KeyPrefix::  | Key::              | Value::                  |
/// +--------------+--------------------+--------------------------+
/// | 0            | OutPoint           | Cell                     |
/// | 32           | ConsumedOutPoint   | Cell                     | * rollback and prune
/// | 64           | CellLockScript     | TxHash                   |
/// | 96           | CellTypeScript     | TxHash                   |
/// | 128          | TxLockScript       | TxHash                   |
/// | 160          | TxTypeScript       | TxHash                   |
/// | 192          | TxHash             | TransactionInputs        | * rollback and prune
/// | 224          | Header             | Transactions             |
/// +--------------+--------------------+--------------------------+
```

**File:** util/indexer/src/indexer.rs (L76-93)
```rust
pub enum KeyPrefix {
    /// OutPoint
    OutPoint = 0,
    /// Consumed OutPoint
    ConsumedOutPoint = 32,
    /// LockScript Cell
    CellLockScript = 64,
    /// TypeScript Cell
    CellTypeScript = 96,
    /// LockScript Tx
    TxLockScript = 128,
    /// TypeScript Tx
    TxTypeScript = 160,
    /// Tx Hash
    TxHash = 192,
    /// Header
    Header = 224,
}
```

**File:** util/indexer/src/indexer.rs (L384-413)
```rust
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
```

**File:** util/indexer/src/indexer.rs (L517-519)
```rust
        if block_number.is_multiple_of(self.prune_interval) {
            self.prune()?;
        }
```

**File:** util/indexer/src/indexer.rs (L752-810)
```rust
    /// Prune useless data
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

**File:** util/indexer/src/service.rs (L494-544)
```rust
                        .expect("stored tx_index"),
                );
                let io_index = u32::from_be_bytes(
                    key[key.len() - 5..key.len() - 1]
                        .try_into()
                        .expect("stored io_index"),
                );
                let io_type = if *key.last().expect("stored io_type") == 0 {
                    IndexerCellType::Input
                } else {
                    IndexerCellType::Output
                };

                if let Some(filter_script) = filter_script.as_ref() {
                    let filter_script_matched = match filter_script_type {
                        IndexerScriptType::Lock => snapshot
                            .get(
                                Key::TxLockScript(
                                    filter_script,
                                    block_number,
                                    tx_index,
                                    io_index,
                                    match io_type {
                                        IndexerCellType::Input => indexer::CellType::Input,
                                        IndexerCellType::Output => indexer::CellType::Output,
                                    },
                                )
                                .into_vec(),
                            )
                            .expect("get TxLockScript should be OK")
                            .is_some(),
                        IndexerScriptType::Type => snapshot
                            .get(
                                Key::TxTypeScript(
                                    filter_script,
                                    block_number,
                                    tx_index,
                                    io_index,
                                    match io_type {
                                        IndexerCellType::Input => indexer::CellType::Input,
                                        IndexerCellType::Output => indexer::CellType::Output,
                                    },
                                )
                                .into_vec(),
                            )
                            .expect("get TxTypeScript should be OK")
                            .is_some(),
                    };
                    if !filter_script_matched {
                        continue;
                    }
```
