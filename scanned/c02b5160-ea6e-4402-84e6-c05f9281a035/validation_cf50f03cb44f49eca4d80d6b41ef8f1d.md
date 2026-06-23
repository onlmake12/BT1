### Title
Tx-Pool Persistence Write Failure Causes Silent Transaction Loss on Shutdown - (File: `tx-pool/src/persisted.rs`)

### Summary
`TxPool::save_into_file()` drains all in-memory transactions **before** the file write completes. If the write or sync fails (e.g., disk full, I/O error), the error is only logged and the node continues shutting down. On restart, both the in-memory pool and the persisted file are empty, permanently losing all pending transactions submitted by RPC callers.

### Finding Description

`save_into_file()` performs three destructive operations in sequence:

1. Opens the file with `truncate(true)`, immediately destroying any previously persisted data.
2. Calls `self.drain_all_transactions()`, which removes every transaction from the in-memory pool map.
3. Calls `file.write_all()` and `file.sync_all()` — if either fails, the error propagates up. [1](#0-0) 

The caller `save_pool()` only logs the error and does not panic, retry, or restore the drained transactions: [2](#0-1) 

`save_pool()` is invoked on the shutdown cancellation signal: [3](#0-2) 

The destructive sequence is:

```
truncate(file)          → old persisted data gone
drain_all_transactions() → in-memory pool cleared
write_all() FAILS       → error logged, node exits
```

On restart, `load_from_file()` finds an empty or truncated file and returns an empty vec, so all previously pending transactions are silently discarded. [4](#0-3) 

### Impact Explanation

Any transaction submitted via the `send_transaction` RPC that was pending (not yet mined) at the time of a failed shutdown save is permanently lost. The node operator and the submitter receive no indication beyond a single log line. Time-sensitive transactions (e.g., those with expiry constraints) will not be resubmitted and will never be mined. This is a direct analog to the Redis bug: a persistence write failure causes the system to lose track of a pending operation it had already accepted.

### Likelihood Explanation

The failure condition — a disk I/O error during shutdown — is realistic:
- **Disk full**: a node running for a long time can exhaust disk space; the shutdown moment is exactly when the largest write (the full pool) is attempted.
- **Filesystem errors**: transient I/O errors, NFS mounts, or permission changes can cause `write_all` or `sync_all` to fail.
- **Induced**: an unprivileged RPC caller can fill the tx-pool with large transactions (up to `max_tx_pool_size`) to maximize the write size, increasing the chance of a partial write failure on constrained systems.

The entry path is fully reachable by an unprivileged `send_transaction` RPC caller.

### Recommendation

Collect transactions into the serialized buffer **before** truncating the file or draining the pool. Use an atomic write pattern (write to a temp file, then rename) so that neither the old persisted data nor the in-memory pool is destroyed until the write is confirmed durable:

```rust
// 1. Serialize first (no mutation yet)
let txs = build_tx_vec(&self.pool_map);
// 2. Write to temp file
let tmp_path = persisted_data_file.with_extension("tmp");
write_and_sync(&tmp_path, txs.as_slice())?;
// 3. Atomic rename
std::fs::rename(&tmp_path, &persisted_data_file)?;
// 4. Only drain after confirmed write
self.drain_all_transactions();
```

If the write fails, the old persisted file remains intact and the in-memory pool is untouched.

### Proof of Concept

1. Submit several transactions via `send_transaction` RPC so the tx-pool is non-empty.
2. Fill the disk to near capacity (or inject a fault via `LD_PRELOAD` to make `write` return `ENOSPC`).
3. Send `SIGTERM` to the CKB node.
4. Observe the log: `"failed to save pool, error: ..."`.
5. Restart the node.
6. Query `get_raw_tx_pool` — the pool is empty; all previously pending transactions are gone.

The root cause is in `save_into_file()`: [5](#0-4) 

`drain_all_transactions()` at line 74 mutates the pool before `write_all` at line 77 has confirmed success, leaving no recovery path if the write fails.

### Citations

**File:** tx-pool/src/persisted.rs (L61-89)
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

        file.write_all(txs.as_slice()).map_err(|err| {
            let errmsg = format!(
                "Failed to write the tx-pool persisted data into file [{persisted_data_file:?}], cause: {err}"
            );
            OtherError::new(errmsg)
        })?;
        file.sync_all().map_err(|err| {
            let errmsg = format!(
                "Failed to sync the tx-pool persisted data file [{persisted_data_file:?}], cause: {err}"
            );
            OtherError::new(errmsg)
        })?;
        Ok(())
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

**File:** tx-pool/src/service.rs (L581-588)
```rust
        let txs = match tx_pool.load_from_file() {
            Ok(txs) => txs,
            Err(e) => {
                error!("{}", e.to_string());
                error!("Failed to load txs from tx-pool persistent data file, all txs are ignored");
                Vec::new()
            }
        };
```

**File:** tx-pool/src/service.rs (L623-628)
```rust
                    _ = signal_receiver.cancelled() => {
                        info!("TxPool is saving, please wait...");
                        process_service.save_pool().await;
                        info!("TxPool process_service exit now");
                        break
                    },
```
