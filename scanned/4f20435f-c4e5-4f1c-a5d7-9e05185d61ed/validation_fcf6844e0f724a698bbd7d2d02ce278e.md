### Title
Non-Atomic Tx-Pool Persistence Truncates Existing Data Before Write Succeeds, Causing Irreversible Transaction Loss on Disk-Full Shutdown — (`tx-pool/src/persisted.rs`)

---

### Summary

`TxPool::save_into_file()` opens the persisted data file with `truncate(true)`, destroying the existing on-disk snapshot before the new write completes. It then calls `drain_all_transactions()`, which empties the in-memory pool, before calling `write_all()`. If `write_all()` fails (e.g., disk full), the error is only logged by the caller `save_pool()` — the process continues to exit. On restart, both the on-disk file and the in-memory pool are empty, and all pending/proposed transactions are permanently lost.

---

### Finding Description

`TxPool::save_into_file()` in `tx-pool/src/persisted.rs` performs a non-atomic, destructive write:

```rust
// Step 1: truncate existing file immediately on open
let mut file = OpenOptions::new()
    .create(true)
    .write(true)
    .truncate(true)          // <-- destroys old data before new data is ready
    .open(&persisted_data_file)
    ...?;

// Step 2: drain ALL transactions from the in-memory pool
let txs = TransactionVec::new_builder()
    .extend(self.drain_all_transactions().iter().map(|tx| tx.data()))
    .build();

// Step 3: write — if this fails, both old file and in-memory pool are gone
file.write_all(txs.as_slice()).map_err(|err| { ... })?;
``` [1](#0-0) 

`drain_all_transactions()` removes every entry from all sub-pools (pending, gap, proposed) and clears the pool map: [2](#0-1) 

The caller `save_pool()` only logs the error and returns normally:

```rust
pub(crate) async fn save_pool(&self) {
    let mut tx_pool = self.tx_pool.write().await;
    if let Err(err) = tx_pool.save_into_file() {
        error!("failed to save pool, error: {:?}", err)  // logged, not fatal
    }
}
``` [3](#0-2) 

`save_pool()` is invoked on every graceful shutdown via the cancellation signal:

```rust
_ = signal_receiver.cancelled() => {
    info!("TxPool is saving, please wait...");
    process_service.save_pool().await;
    info!("TxPool process_service exit now");
    break
},
``` [4](#0-3) 

On restart, `load_from_file()` finds the file exists but is empty/truncated. Parsing an empty buffer as `TransactionVecReader` fails, the error is logged, and an empty `Vec` is returned — the pool starts with zero transactions: [5](#0-4) 

---

### Impact Explanation

When the disk is full at shutdown time:

1. `truncate(true)` destroys the previously-persisted snapshot the moment the file is opened.
2. `drain_all_transactions()` removes every pending, gap, and proposed transaction from the live in-memory pool.
3. `write_all()` fails with an I/O error (no space left on device).
4. The error is logged; the process exits.
5. On restart, the persisted file is empty and the pool is empty — all transactions that were queued for inclusion in upcoming blocks are permanently lost.

Transactions in the `Proposed` state (already included in a block proposal window) are particularly affected: they will not be re-included in the next block template, delaying or preventing their on-chain confirmation until users manually resubmit them.

This is a direct analog of the reported pattern: in-memory state is mutated (`drain_all_transactions`) before persistence is confirmed, and a persistence failure is silently swallowed rather than aborting the process.

---

### Likelihood Explanation

The disk-full condition is realistic on long-running nodes: RocksDB compaction, the freezer's append-only cold storage, and log rotation all compete for disk space. A node operator who does not monitor disk usage will hit this silently. The failure is especially insidious because the log line `"TxPool is saving, please wait..."` appears, followed by `"TxPool process_service exit now"` — giving no visible indication that the save failed and data was lost.

---

### Recommendation

1. **Use an atomic write**: write to a temporary file in the same directory, then `rename()` it over the destination. This matches the `WriteFileAtomic` pattern referenced in the original report and is already used by the peer-store (`dump_to_dir` writes to a `tmp/` subdirectory then calls `move_file`). [6](#0-5) 

2. **Do not drain the in-memory pool before confirming the write**: collect the transactions into a serialized buffer first, write to a temp file, sync, rename — only then (or never, since it is shutdown) discard the in-memory state.

3. **Treat a persistence failure as fatal**: consistent with the upstream fix ("abort the application if persistence fails"), the node should log a clear error and exit with a non-zero status rather than silently continuing, so operators are alerted and can recover before data is lost.

---

### Proof of Concept

1. Start a CKB node and submit several transactions so the tx-pool is non-empty.
2. Fill the data partition to near-capacity (e.g., with a large file).
3. Send `SIGTERM` to the node to trigger graceful shutdown.
4. Observe in the log: `"failed to save pool, error: ..."` (disk full).
5. Restart the node.
6. Query `tx_pool_info` via RPC — `pending` and `proposed` counts are `0x0`; all previously queued transactions are gone.

The root cause is the sequence at `tx-pool/src/persisted.rs` lines 61–82: `truncate(true)` on open + `drain_all_transactions()` before `write_all()` + error only logged by the caller at `tx-pool/src/process.rs` lines 934–935. [7](#0-6)

### Citations

**File:** tx-pool/src/persisted.rs (L57-90)
```rust
    pub(crate) fn save_into_file(&mut self) -> Result<(), AnyError> {
        let mut persisted_data_file = self.config.persisted_data.clone();
        persisted_data_file.set_extension(format!("v{VERSION}"));

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
    }
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

**File:** network/src/peer_store/peer_store_db.rs (L232-251)
```rust
    /// Dump all info to disk
    pub fn dump_to_dir<P: AsRef<Path>>(&self, path: P) -> Result<(), Error> {
        // create dir
        create_dir_all(&path)?;
        // dump file to a temporary sub-directory
        let tmp_dir = path.as_ref().join("tmp");
        create_dir_all(&tmp_dir)?;
        let tmp_addr_manager = tmp_dir.join(DEFAULT_ADDR_MANAGER_DB);
        let tmp_ban_list = tmp_dir.join(DEFAULT_BAN_LIST_DB);
        let tmp_anchors_list = tmp_dir.join(DEFAULT_ANCHORS_DB);
        self.addr_manager().dump(dump_open(&tmp_addr_manager)?)?;
        move_file(
            tmp_addr_manager,
            path.as_ref().join(DEFAULT_ADDR_MANAGER_DB),
        )?;
        self.ban_list().dump(dump_open(&tmp_ban_list)?)?;
        move_file(tmp_ban_list, path.as_ref().join(DEFAULT_BAN_LIST_DB))?;
        self.anchors().dump(dump_open(&tmp_anchors_list)?)?;
        move_file(tmp_anchors_list, path.as_ref().join(DEFAULT_ANCHORS_DB))?;
        Ok(())
```
