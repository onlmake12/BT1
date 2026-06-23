[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** util/indexer/src/indexer.rs (L198-209)
```rust
            Value::Transactions(txs) => {
                txs.iter().for_each(|(tx_hash, outputs_len, tx_index)| {
                    encoded.extend_from_slice(tx_hash.as_slice());
                    encoded.extend_from_slice(&(outputs_len).to_le_bytes());
                    if let Some(i) = tx_index {
                        encoded.extend_from_slice(&i.to_le_bytes());
                    }
                });
            }
        }
        encoded
    }
```

**File:** util/indexer/src/indexer.rs (L233-233)
```rust
        let chunk_size = if filtered { 32 + 4 + 4 } else { 32 + 4 }; // hash(32) + outputs_len(4) + tx_index(4)
```

**File:** util/indexer/src/indexer.rs (L317-327)
```rust
    fn append(&self, block: &BlockView) -> Result<(), Error> {
        let mut batch = self.store.batch()?;
        let transactions = block.transactions();
        let pool = self.pool.as_ref().map(|p| p.write().expect("acquire lock"));
        if !self.custom_filters.is_block_filter_match(block) {
            batch.put_kv(Key::Header(block.number(), &block.hash(), true), vec![])?;
            batch.commit()?;

            if let Some(mut pool) = pool {
                pool.transactions_committed(&transactions);
            }
```

**File:** util/indexer/src/indexer.rs (L494-510)
```rust
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

**File:** util/indexer/src/indexer.rs (L534-541)
```rust
        if let Some((block_number, block_hash, filtered, txs)) = iter.next().map(|(key, value)| {
            let filtered = key.len() == 42 && key[41] == 1;
            (
                BlockNumber::from_be_bytes(key[1..9].try_into().expect("stored block key")),
                Byte32::from_slice(&key[9..41]).expect("stored block key"),
                filtered,
                Value::parse_transactions_value(&value, filtered),
            )
```
