### Title
Quadratic Work in Indexer `append` Due to Unbounded Linear Scan Over Block Transactions for Within-Block Cell Dependencies — (`File: util/indexer/src/indexer.rs`)

---

### Summary

The `Indexer::append` function in `util/indexer/src/indexer.rs` contains a triple-nested loop pattern. For every input of every transaction in a block, when the referenced cell was created within the same block (a within-block dependency), the code performs a full linear scan over all transactions in the block to locate the producing transaction. This results in O(T² × I) work per block, where T is the number of transactions and I is the average number of within-block inputs per transaction. An unprivileged transaction sender can craft a block full of chained transactions to force this worst-case behavior, causing the indexer to stall or fall significantly behind the chain tip.

---

### Finding Description

In `util/indexer/src/indexer.rs`, the `append` method processes each block by iterating over all transactions and their inputs: [1](#0-0) 

The outer loop iterates over every transaction in the block (O(T)). The inner loop iterates over every input of each transaction (O(I)). For each input, the code first attempts to look up the referenced cell from the committed store:

```rust
if let Some(stored_live_cell) = self.store.get(&key_vec)?.or_else(|| {
    transactions
        .iter()
        .enumerate()
        .find(|(_i, tx)| tx.hash() == out_point.tx_hash())
        ...
})
``` [2](#0-1) 

The `self.store.get(&key_vec)` call reads from the **committed** store. The batch being built in this same call has not yet been committed: [3](#0-2) 

Therefore, for any input whose referenced cell was created by an earlier transaction **in the same block** (a within-block dependency), `self.store.get` returns `None`, and the `or_else` closure executes a full O(T) linear scan over all block transactions via `transactions.iter().enumerate().find(...)`.

The total complexity for a block with T transactions each having I within-block inputs is:

```
O(T) outer × O(I) inner × O(T) scan = O(T² × I)
```

There is no cap or early-exit on this scan beyond the block size limit.

---

### Impact Explanation

The indexer is a production service used by wallets, dApps, and tooling to query live cells and transaction history. When the indexer processes a block containing a long chain of within-block dependent transactions, the `append` call consumes O(T²) CPU time instead of O(T). With CKB's block size limit (~597 KB) and a minimum transaction size of ~200 bytes, a block can contain roughly 3,000 transactions. A fully chained block would cause ~9 million hash comparisons instead of ~3,000, a ~3,000× slowdown per block. Sustained over multiple blocks, the indexer falls behind the chain tip and becomes unavailable to all consumers, constituting a practical DoS of the indexer service.

---

### Likelihood Explanation

The attack requires only that a transaction sender submit a chain of N transactions (each spending the previous transaction's output) and have them included in a single block. This is a standard and economically rational pattern (e.g., batch payments, UTXO consolidation). The attacker pays O(N) in transaction fees while imposing O(N²) indexer work. No special privileges, keys, or majority hashpower are required. Any miner willing to include the chain (which is valid by consensus rules) triggers the worst case. The pattern is indistinguishable from legitimate use.

---

### Recommendation

Replace the O(T) linear scan with an O(1) lookup by building an in-memory map of `OutPoint → cell value` from the current block's outputs **before** processing inputs. This map is populated once at the start of `append` (O(T × outputs_per_tx)) and consulted in O(1) per input lookup, reducing the total complexity to O(T × I):

```rust
// Build within-block cell map once before the main loop
let mut block_cell_map: HashMap<OutPoint, Vec<u8>> = HashMap::new();
for (i, tx) in transactions.iter().enumerate() {
    for (idx, (output, data)) in tx.outputs_with_data_iter().enumerate() {
        let out_point = OutPoint::new(tx.hash(), idx as u32);
        block_cell_map.insert(out_point, Value::Cell(block_number, i as u32, &output, &data).into());
    }
}

// Then in the input loop, replace the or_else scan:
if let Some(stored_live_cell) = self.store.get(&key_vec)?
    .or_else(|| block_cell_map.get(&out_point).cloned())
{
    ...
}
```

---

### Proof of Concept

1. Attacker constructs a chain of N = 3,000 transactions: `tx[0]` creates one output; `tx[k]` spends `tx[k-1]`'s output and creates a new one.
2. Attacker submits all 3,000 transactions to the mempool and pays fees to have them included in a single block (valid by CKB consensus rules; no ancestor limit applies at the block level).
3. When the indexer calls `append` on this block:
   - For `tx[1]`: 1 input → `store.get` misses → scans 3,000 txs → 3,000 comparisons.
   - For `tx[2]`: 1 input → `store.get` misses → scans 3,000 txs → 3,000 comparisons.
   - …
   - For `tx[2999]`: 1 input → `store.get` misses → scans 3,000 txs → 3,000 comparisons.
   - **Total: ~9,000,000 hash comparisons** for a single block instead of ~3,000.
4. Repeated over consecutive blocks, the indexer's processing time per block grows to seconds or minutes, causing it to fall behind the chain tip and become unavailable.

### Citations

**File:** util/indexer/src/indexer.rs (L318-320)
```rust
        let mut batch = self.store.batch()?;
        let transactions = block.transactions();
        let pool = self.pool.as_ref().map(|p| p.write().expect("acquire lock"));
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
