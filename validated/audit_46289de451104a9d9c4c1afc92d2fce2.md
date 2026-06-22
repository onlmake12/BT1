The bug is real. Let me trace the exact execution path to confirm.

### Title
Indexer Pool `dead_cells` Reference-Count Missing Causes RBF Race to Expose Pending-Spent Cell as Live — (`util/indexer-sync/src/pool.rs`)

---

### Summary

`Pool` tracks consumed outpoints in a plain `HashSet<OutPoint>`. When RBF causes T2 to replace T1 (both spending the same `OutPoint O`), the async notification pipeline can deliver the `new_transaction(T2)` event before `transaction_rejected(T1)`. Because `HashSet::insert` is a no-op on a duplicate, the subsequent `transaction_rejected(T1)` unconditionally removes `O`, leaving `dead_cells` empty while T2 is still pending. `get_cells` then returns `O` as a live cell.

---

### Finding Description

**Root cause — no reference counting in `Pool`:**

`dead_cells` is a `HashSet<OutPoint>`. Both `new_transaction` and `transaction_rejected` operate on the raw set with no multiplicity tracking: [1](#0-0) 

**Notification dispatch — two independent fire-and-forget spawns:**

Inside `submit_entry`, `process_rbf` is called first (line 136), which removes T1 and fires the reject callback; `_submit_entry` is called second (line 137), which adds T2 and fires the pending callback: [2](#0-1) 

Each callback calls `notify_reject_transaction` / `notify_new_transaction`, both of which spawn independent async tasks onto the tokio runtime: [3](#0-2) [4](#0-3) 

**Consumer — `tokio::select!` over two separate channels:**

`PoolService::index_tx_pool` selects over `new_transaction_receiver` and `reject_transaction_receiver` independently. Whichever channel has a message ready wins: [5](#0-4) 

**The race:**

| Step | Event processed | `dead_cells` |
|------|----------------|--------------|
| Initial (T1 accepted) | — | `{O}` |
| **Buggy ordering:** `new(T2)` arrives first | `insert(O)` → no-op | `{O}` |
| `reject(T1)` arrives second | `remove(O)` | `{}` ← T2 still pending |

**Impact site — `get_cells` reads `dead_cells` to exclude pool-consumed cells:** [6](#0-5) 

With `O` absent from `dead_cells`, the SQL filter `NOT IN (...)` does not exclude `O`, so `get_cells` returns `O` as a live cell while T2 is still pending and spending it.

---

### Impact Explanation

Any client using the `get_cells` or `get_cells_capacity` RPC while T2 is pending will observe `O` as an unspent live cell. Applications that rely on the indexer for wallet balance or UTXO selection will compute incorrect available balances and may construct transactions that conflict with T2, leading to unexpected rejections or incorrect state presentation to end users.

---

### Likelihood Explanation

The race is triggered by any successful RBF submission via the standard `send_transaction` RPC — no privilege required. The two async spawns land on separate tokio channels consumed by a single `select!` loop; under any non-trivial scheduler load the "new before reject" ordering is reachable. The window persists until T2 is committed or itself rejected.

---

### Recommendation

Replace `HashSet<OutPoint>` with a reference-counted map (`HashMap<OutPoint, u32>`). Increment the counter in `new_transaction`, decrement in `transaction_rejected` / `transaction_committed`, and only remove the entry when the counter reaches zero. This makes the invariant hold regardless of notification ordering.

---

### Proof of Concept

```rust
// Simulate the buggy ordering directly on Pool
let mut pool = Pool::default();
let out_point = /* any OutPoint */;

// T1 accepted
pool.new_transaction(&tx1);          // dead_cells = {O}

// RBF: new(T2) notification arrives before reject(T1)
pool.new_transaction(&tx2);          // dead_cells = {O}  (no-op, same OutPoint)
pool.transaction_rejected(&tx1);     // dead_cells = {}   ← BUG

// T2 is still "pending" but:
assert!(!pool.is_consumed_by_pool_tx(&out_point)); // passes — O wrongly absent
// get_cells will now return O as live
```

### Citations

**File:** util/indexer-sync/src/pool.rs (L20-44)
```rust
pub struct Pool {
    dead_cells: HashSet<OutPoint>,
}

impl Pool {
    /// the tx has been committed in a block, it should be removed from pending dead cells
    pub fn transaction_committed(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.remove(&input.previous_output());
        }
    }

    /// the tx has been rejected for some reason, it should be removed from pending dead cells
    pub fn transaction_rejected(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.remove(&input.previous_output());
        }
    }

    /// a new tx is submitted to the pool, mark its inputs as dead cells
    pub fn new_transaction(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.insert(input.previous_output());
        }
    }
```

**File:** util/indexer-sync/src/pool.rs (L123-143)
```rust
            loop {
                tokio::select! {
                    Some(tx_entry) = new_transaction_receiver.recv() => {
                        if let Some(pool) = service.pool.as_ref() {
                            pool.write().expect("acquire lock").new_transaction(&tx_entry.transaction);
                        }
                    }
                    Some((tx_entry, _reject)) = reject_transaction_receiver.recv() => {
                        if let Some(pool) = service.pool.as_ref() {
                            pool.write()
                            .expect("acquire lock")
                            .transaction_rejected(&tx_entry.transaction);
                        }
                    }
                    _ = stop.cancelled() => {
                        info!("index_tx_pool received exit signal, exit now");
                        break
                    },
                    else => break,
                }
            }
```

**File:** tx-pool/src/process.rs (L136-137)
```rust
                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;
```

**File:** notify/src/lib.rs (L505-511)
```rust
    pub fn notify_new_transaction(&self, tx_entry: PoolTransactionEntry) {
        let new_transaction_notifier = self.new_transaction_notifier.clone();
        self.handle.spawn(async move {
            if let Err(e) = new_transaction_notifier.send(tx_entry).await {
                error!("notify_new_transaction channel is closed: {}", e);
            }
        });
```

**File:** notify/src/lib.rs (L549-555)
```rust
    pub fn notify_reject_transaction(&self, tx_entry: PoolTransactionEntry, reject: Reject) {
        let reject_transaction_notifier = self.reject_transaction_notifier.clone();
        self.handle.spawn(async move {
            if let Err(e) = reject_transaction_notifier.send((tx_entry, reject)).await {
                error!("notify_reject_transaction channel is closed: {}", e);
            }
        });
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L109-134)
```rust
        // filter cells in pool
        let mut dead_cells = Vec::new();
        if let Some(pool) = self
            .pool
            .as_ref()
            .map(|pool| pool.read().expect("acquire lock"))
        {
            dead_cells = pool
                .dead_cells()
                .map(|out_point| {
                    let tx_hash: H256 = out_point.tx_hash().into();
                    (tx_hash.as_bytes().to_vec(), out_point.index().into())
                })
                .collect::<Vec<(_, u32)>>()
        }
        if !dead_cells.is_empty() {
            let placeholders = dead_cells
                .iter()
                .map(|(_, output_index)| {
                    let placeholder = format!("(${}, {})", param_index, output_index);
                    param_index += 1;
                    placeholder
                })
                .collect::<Vec<_>>()
                .join(",");
            query_builder.and_where(format!("(tx_hash, output_index) NOT IN ({})", placeholders));
```
