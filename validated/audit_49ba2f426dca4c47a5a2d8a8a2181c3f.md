### Title
Unbounded Nested Loop in Indexer `append()` Causes O(T²) CPU Exhaustion on Intra-Block Dependencies — (`File: util/indexer/src/indexer.rs`)

---

### Summary

The `Indexer::append()` function in `util/indexer/src/indexer.rs` contains a triple-nested loop. For each input of each transaction in a block, when the spent cell was created within the same block (an intra-block dependency), the code performs a full linear scan over all transactions in the block to locate the producer. A miner or block relayer can craft a valid block with many transactions that all have intra-block dependencies, triggering O(T² × I) work in the indexer thread, where T is the number of transactions and I is the average number of inputs per transaction.

---

### Finding Description

Inside `Indexer::append()`, the outer loop iterates over every transaction in the block, and the middle loop iterates over every input of each transaction:

```rust
for (tx_index, tx) in transactions.iter().enumerate() {
    if tx_index > 0 {
        for (input_index, input) in tx.inputs().into_iter().enumerate() {
            let out_point = input.previous_output();
            let key_vec = Key::OutPoint(&out_point).into_vec();

            if let Some(stored_live_cell) = self.store.get(&key_vec)?.or_else(|| {
                transactions          // <-- inner linear scan over ALL block txs
                    .iter()
                    .enumerate()
                    .find(|(_i, tx)| tx.hash() == out_point.tx_hash())
                    ...
            }) { ... }
        }
    }
}
``` [1](#0-0) 

The `or_else` fallback — which performs the inner `transactions.iter().enumerate().find(...)` scan — is invoked whenever `self.store.get(&key_vec)?` returns `None`. This happens precisely when the cell being spent was **created in the same block** (intra-block dependency), because the cell has not yet been written to the persistent store at the time of the scan. For each such input, the code performs a full O(T) scan over all block transactions.

The total work is therefore **O(T × I × T) = O(T² × I)** in the worst case, where T is the number of transactions in the block and I is the average number of inputs per transaction.

CKB's consensus enforces `max_block_bytes` (597 KB on mainnet) and `max_block_cycles`, but does **not** bound the number of intra-block dependencies. A minimal transaction (1 input, 1 output, no script) is approximately 100 bytes, allowing up to ~5,970 transactions per block. With all transactions forming a dependency chain, the inner scan executes ~5,970 times per transaction, yielding ~35 million hash comparisons per block.

---

### Impact Explanation

The indexer's `append()` is called synchronously for every new block committed to the chain. A single crafted block with T transactions all having intra-block dependencies causes the indexer thread to perform O(T²) work before returning. This stalls the indexer service, causing:

- The indexer to fall arbitrarily far behind the chain tip.
- All indexer-backed RPC calls (`get_cells`, `get_transactions`, `get_cells_capacity`, etc.) to return stale or unavailable data.
- Sustained CPU saturation on the indexer thread for the duration of processing.

The impact is a **denial-of-service on the indexer service**, which is a production component used by wallets, dApps, and block explorers that rely on the CKB node's indexer RPC.

---

### Likelihood Explanation

**High.** Any miner who produces a valid block can include a chain of transactions where each transaction spends an output from the previous one in the same block. This is a fully valid CKB block structure — intra-block dependencies are explicitly supported by the `BlockCellProvider` mechanism. No special privilege, leaked key, or majority hashpower is required. A single block is sufficient to trigger the condition.

---

### Recommendation

Replace the inner linear scan with a pre-built `HashMap<Byte32, usize>` mapping transaction hashes to their indices, constructed once before the outer loop. This reduces the overall complexity from O(T² × I) to O(T × I):

```rust
// Build a tx_hash -> index map once before the outer loop
let tx_hash_index: HashMap<Byte32, usize> = transactions
    .iter()
    .enumerate()
    .map(|(i, tx)| (tx.hash(), i))
    .collect();

// Then replace the inner find(...) with:
tx_hash_index.get(&out_point.tx_hash()).map(|&i| { ... })
```

This is the standard fix for the nested-loop-over-same-collection pattern.

---

### Proof of Concept

1. Construct a block with T = 5,000 transactions where transaction `i` spends output 0 of transaction `i-1` (a linear dependency chain). All transactions use an `always_success` lock script so they are valid.
2. Submit the block to a CKB node with the indexer enabled.
3. Observe that `Indexer::append()` performs approximately 5,000 × 5,000 = 25,000,000 hash comparisons before returning.
4. The indexer thread is blocked for the entire duration; subsequent blocks queue up and the indexer tip diverges from the chain tip.

The inner scan path is confirmed at: [2](#0-1) 

The outer transaction loop and middle input loop are at: [3](#0-2) 

The `append()` entry point called for every new block: [4](#0-3)

### Citations

**File:** util/indexer/src/indexer.rs (L317-330)
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

            return Ok(());
        }
```

**File:** util/indexer/src/indexer.rs (L334-362)
```rust
        for (tx_index, tx) in transactions.iter().enumerate() {
            let tx_index = tx_index as u32;
            let tx_hash = tx.hash();
            let mut tx_matched = false;
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
```
