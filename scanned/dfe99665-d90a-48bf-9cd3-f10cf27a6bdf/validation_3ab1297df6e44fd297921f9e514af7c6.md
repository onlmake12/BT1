Now I have all the information needed. Let me trace the full execution path.

**Key facts established:**

1. The message processing loop spawns each message as a **separate concurrent async task** — two `SavePool` messages run concurrently.
2. `save_pool()` acquires `self.tx_pool.write().await` — a tokio async RwLock, so the two tasks **serialize** on this lock.
3. `save_into_file()` opens the file with `truncate(true)` **before** calling `drain_all_transactions()`.
4. `drain_all_transactions()` is **destructive** — it calls `remove_by_status` for Pending/Gap and then `pool_map.clear()`, leaving the pool empty.

The race is not a true data race (the lock prevents that), but a **logical sequence bug**: Task 1 drains the pool and writes all txs; Task 2 then drains an already-empty pool and **overwrites the valid file with zero transactions**.

---

### Title
Double `SavePool` Empties Persisted Tx-Pool File via Sequential Drain-and-Overwrite — (`tx-pool/src/persisted.rs`)

### Summary

Two concurrent local RPC `save_pool` calls serialize on the tx-pool write lock. The first call correctly drains and saves all transactions. The second call then drains an already-empty pool and overwrites the file with a zero-transaction molecule blob, causing the node to load an empty pool on the next restart.

### Finding Description

The message dispatch loop spawns each incoming `Message` as an independent async task: [1](#0-0) 

Two concurrent `Message::SavePool` requests therefore run as two separate tasks. Each calls `save_pool()`, which acquires the exclusive write lock: [2](#0-1) 

The lock serializes them, but that is exactly the problem. `save_into_file()` opens the file with `truncate(true)` and then calls `drain_all_transactions()`: [3](#0-2) 

`drain_all_transactions()` is fully destructive — it removes all entries by status and then clears the pool map: [4](#0-3) 

Execution sequence:
1. Task 1 acquires write lock → truncates file → drains pool (N txs) → writes N txs → releases lock.
2. Task 2 acquires write lock → **truncates file** (erasing Task 1's valid save) → drains now-empty pool (0 txs) → writes 0 txs → releases lock.

The final file contains a valid but empty `TransactionVec` molecule blob. On the next node restart, `load_from_file()` successfully parses it and returns an empty `Vec`, silently discarding all previously pending transactions. [5](#0-4) 

### Impact Explanation

All pending/proposed transactions that were in the pool at save time are permanently lost after restart. The node operator believes the pool was saved (both RPC calls return success), but the persisted file is empty. This is a silent data-loss condition with no error logged.

### Likelihood Explanation

Any process with local RPC access can trigger this by issuing two concurrent `save_pool` JSON-RPC calls. No authentication, no special privilege beyond local RPC access (which is the defined scope). The `save_pool` RPC is also called automatically on shutdown signal: [6](#0-5) 

A local attacker who sends a `SavePool` RPC call at the same moment the node begins its shutdown sequence will reliably trigger this condition.

### Recommendation

- Do not use `drain_all_transactions` for persistence; use a read-only snapshot of the pool instead, leaving the pool intact.
- Alternatively, write to a temporary file and atomically rename it over the destination, so a second concurrent write cannot corrupt a completed first write.
- Add a dedicated save-in-progress flag or a separate save mutex to prevent concurrent saves entirely.

### Proof of Concept

```
1. Start a CKB node with several pending transactions in the pool.
2. From two threads simultaneously, send:
     POST /  {"jsonrpc":"2.0","method":"save_pool","params":[],"id":1}
3. Wait for both to return success.
4. Inspect the persisted data file (*.v1): it will contain a valid but
   zero-transaction molecule TransactionVec blob.
5. Restart the node; observe that the tx-pool loads empty despite both
   save_pool calls having returned success.
```

### Citations

**File:** tx-pool/src/service.rs (L619-621)
```rust
                    Some(message) = receiver.recv() => {
                        let service_clone = process_service.clone();
                        handle_clone.spawn(process(service_clone, message));
```

**File:** tx-pool/src/service.rs (L623-627)
```rust
                    _ = signal_receiver.cancelled() => {
                        info!("TxPool is saving, please wait...");
                        process_service.save_pool().await;
                        info!("TxPool process_service exit now");
                        break
```

**File:** tx-pool/src/process.rs (L932-939)
```rust
    pub(crate) async fn save_pool(&self) {
        let mut tx_pool = self.tx_pool.write().await;
        if let Err(err) = tx_pool.save_into_file() {
            error!("failed to save pool, error: {:?}", err)
        } else {
            info!("TxPool saved successfully")
        }
    }
```

**File:** tx-pool/src/persisted.rs (L17-54)
```rust
    pub(crate) fn load_from_file(&self) -> Result<Vec<TransactionView>, AnyError> {
        let mut persisted_data_file = self.config.persisted_data.clone();
        persisted_data_file.set_extension(format!("v{VERSION}"));

        if persisted_data_file.exists() {
            let mut file = OpenOptions::new()
                .read(true)
                .open(&persisted_data_file)
                .map_err(|err| {
                    let errmsg = format!(
                        "Failed to open the tx-pool persisted data file [{persisted_data_file:?}], cause: {err}"
                    );
                    OtherError::new(errmsg)
                })?;
            let mut buffer = Vec::new();
            file.read_to_end(&mut buffer).map_err(|err| {
                let errmsg = format!(
                    "Failed to read the tx-pool persisted data file [{persisted_data_file:?}], cause: {err}"
                );
                OtherError::new(errmsg)
            })?;

            let persisted_data = TransactionVecReader::from_slice(&buffer)
                .map_err(|err| {
                    let errmsg = format!(
                        "The tx-pool persisted data file [{persisted_data_file:?}] is broken, cause: {err}"
                    );
                    OtherError::new(errmsg)
                })?
                .to_entity();

            Ok(persisted_data
                .into_iter()
                .map(|tx| tx.into_view())
                .collect())
        } else {
            Ok(Vec::new())
        }
```

**File:** tx-pool/src/persisted.rs (L61-75)
```rust
        let mut file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&persisted_data_file)
            .map_err(|err| {
                let errmsg = format!(
                    "Failed to open the tx-pool persisted data file [{persisted_data_file:?}], cause: {err}"
                );
                OtherError::new(errmsg)
            })?;

        let txs = TransactionVec::new_builder()
            .extend(self.drain_all_transactions().iter().map(|tx| tx.data()))
            .build();
```

**File:** tx-pool/src/pool.rs (L489-514)
```rust
    pub(crate) fn drain_all_transactions(&mut self) -> Vec<TransactionView> {
        let mut txs = TxSelector::new(&self.pool_map)
            .txs_to_commit(usize::MAX, Cycle::MAX)
            .0
            .into_iter()
            .map(|tx_entry| tx_entry.into_transaction())
            .collect::<Vec<_>>();
        let mut pending = self
            .pool_map
            .entries
            .remove_by_status(&Status::Pending)
            .into_iter()
            .map(|e| e.inner.into_transaction())
            .collect::<Vec<_>>();
        txs.append(&mut pending);
        let mut gap = self
            .pool_map
            .entries
            .remove_by_status(&Status::Gap)
            .into_iter()
            .map(|e| e.inner.into_transaction())
            .collect::<Vec<_>>();
        txs.append(&mut gap);
        self.pool_map.clear();
        txs
    }
```
