All cited code references are confirmed in the repository. Let me verify the key facts:

1. **Concurrent task spawning** — confirmed at `service.rs` L619-621 [1](#0-0) 
2. **Write lock acquisition in `save_pool()`** — confirmed at `process.rs` L932-933 [2](#0-1) 
3. **`truncate(true)` before `drain_all_transactions()`** — confirmed at `persisted.rs` L61-74 [3](#0-2) 
4. **Destructive drain with `pool_map.clear()`** — confirmed at `pool.rs` L489-513 [4](#0-3) 
5. **Shutdown path calls `save_pool()` directly** — confirmed at `service.rs` L623-627 [5](#0-4) 

The logical sequence bug is real and fully confirmed by the code. The impact fits the "Note" category (local RPC data loss).

---

Audit Report

## Title
Double `save_pool` Empties Persisted Tx-Pool File via Sequential Drain-and-Overwrite — (`tx-pool/src/persisted.rs`)

## Summary

Two concurrent local RPC `save_pool` calls serialize on the tx-pool write lock. The first call correctly drains and saves all transactions. The second call then drains an already-empty pool and overwrites the file with a zero-transaction molecule blob. On the next node restart, `load_from_file()` successfully parses the valid-but-empty blob and silently returns an empty pool, discarding all previously pending transactions.

## Finding Description

The message dispatch loop in `service.rs` spawns each incoming `Message` as an independent async task:

```rust
// service.rs L619-621
Some(message) = receiver.recv() => {
    let service_clone = process_service.clone();
    handle_clone.spawn(process(service_clone, message));
```

Two concurrent `Message::SavePool` requests therefore run as two separate tasks. Each calls `save_pool()`, which acquires the exclusive write lock on the pool:

```rust
// process.rs L932-933
pub(crate) async fn save_pool(&self) {
    let mut tx_pool = self.tx_pool.write().await;
```

The lock serializes the two tasks, but that is exactly the problem. `save_into_file()` opens the file with `truncate(true)` and then calls `drain_all_transactions()`:

```rust
// persisted.rs L61-74
let mut file = OpenOptions::new()
    .create(true).write(true).truncate(true)
    .open(&persisted_data_file)...;
let txs = TransactionVec::new_builder()
    .extend(self.drain_all_transactions().iter().map(|tx| tx.data()))
    .build();
```

`drain_all_transactions()` is fully destructive — it removes all entries by status and then calls `self.pool_map.clear()`, leaving the pool empty for any subsequent caller.

Execution sequence:
1. Task 1 acquires write lock → truncates file → drains pool (N txs) → writes N txs → releases lock.
2. Task 2 acquires write lock → **truncates file** (erasing Task 1's valid save) → drains now-empty pool (0 txs) → writes 0 txs → releases lock.

The final file contains a valid but empty `TransactionVec` molecule blob. `load_from_file()` successfully parses it and returns an empty `Vec`, silently discarding all previously pending transactions.

The shutdown path also calls `save_pool()` directly (not via the message channel), creating a natural race window between an in-flight RPC `SavePool` task and the shutdown-triggered save.

## Impact Explanation

All pending/proposed transactions in the pool at save time are permanently lost after restart. Both RPC calls return success, no error is logged, and the node operator has no indication the persisted file is empty. This is a concrete, silent data-loss condition triggered via local RPC, fitting the **Note (0–500 points): Any local RPC API crash** category (data-loss variant of a local RPC malfunction).

## Likelihood Explanation

Any process with local RPC access can trigger this by issuing two concurrent `save_pool` JSON-RPC calls. No authentication or elevated privilege is required beyond local RPC access, which is the defined scope. The most reliable trigger is sending one `save_pool` RPC call at the same moment the node begins its shutdown sequence, since the shutdown handler calls `save_pool()` directly and concurrently with any in-flight RPC-dispatched task.

## Recommendation

- Replace `drain_all_transactions()` in `save_into_file()` with a read-only snapshot of the pool, leaving the pool intact after saving.
- Write to a temporary file and atomically rename it over the destination (`rename(tmp, dest)`), so a second concurrent write cannot corrupt a completed first write.
- Add a dedicated save-in-progress flag or a separate save mutex to prevent concurrent saves entirely.

## Proof of Concept

```
1. Start a CKB node with several pending transactions in the pool.
2. From two threads simultaneously, send:
     POST /  {"jsonrpc":"2.0","method":"save_pool","params":[],"id":1}
3. Wait for both to return {"result": null} (success).
4. Inspect the persisted data file (*.v1): it will contain a valid but
   zero-transaction molecule TransactionVec blob (4-byte length prefix
   with value 4, no transaction entries).
5. Restart the node; observe that the tx-pool loads empty despite both
   save_pool calls having returned success.
   
Alternatively: send one save_pool RPC call and simultaneously send
SIGTERM to the node to race the RPC task against the shutdown-triggered
save_pool() call in the signal handler.
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

**File:** tx-pool/src/process.rs (L932-933)
```rust
    pub(crate) async fn save_pool(&self) {
        let mut tx_pool = self.tx_pool.write().await;
```

**File:** tx-pool/src/persisted.rs (L61-74)
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
```

**File:** tx-pool/src/pool.rs (L489-513)
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
```
