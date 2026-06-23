### Title
Tx-Pool Pending Transactions Persisted to Disk Without Encryption or Restrictive File Permissions - (File: `tx-pool/src/persisted.rs`)

### Summary
When a CKB node shuts down gracefully, all pending (unconfirmed) transactions in the mempool are serialized and written to a plaintext file on disk (`data_dir/tx_pool/persisted_data.v1`) with no encryption and no restrictive OS-level file permissions. Any local user or process with read access to the CKB data directory can parse this file and extract full transaction details — inputs, outputs, lock scripts (addresses), capacities (amounts), witnesses, and cell data — even after the node has been removed.

### Finding Description
`TxPool::save_into_file()` in `tx-pool/src/persisted.rs` serializes all pending transactions using molecule encoding and writes them to disk using a plain `OpenOptions` call with no permission restriction:

```rust
let mut file = OpenOptions::new()
    .create(true)
    .write(true)
    .truncate(true)
    .open(&persisted_data_file)  // no chmod, no 0o600/0o400
    ...
file.write_all(txs.as_slice())  // raw molecule bytes, no encryption
``` [1](#0-0) 

The file is created with the process's default umask (typically `0o644` or `0o664` on Linux), making it world-readable or group-readable by default. The molecule-encoded binary format is not encryption — it is a well-documented, publicly specified serialization format that any party with the CKB types library can fully decode.

This stands in direct contrast to how CKB handles the network secret key. `write_secret_to_file()` in `util/app-config/src/configs/network.rs` explicitly sets `0o400` (owner-read-only) on Unix and `readonly` on Windows immediately after writing:

```rust
file.set_permissions(fs::Permissions::from_mode(0o400))  // Unix
permissions.set_readonly(true);                           // non-Unix
``` [2](#0-1) 

No equivalent protection is applied to the tx-pool persisted data file.

The file path is resolved at startup via `TxPoolConfig::adjust()`, defaulting to `<data_dir>/tx_pool/persisted_data.v1`: [3](#0-2) [4](#0-3) 

The file is written on every graceful shutdown (signal cancellation triggers `save_pool()`) and also on explicit `save_pool` RPC calls: [5](#0-4) [6](#0-5) 

### Impact Explanation
A local unprivileged user or co-resident process on the same machine as the CKB node can read `<data_dir>/tx_pool/persisted_data.v1` and decode it using the public molecule schema to extract:

- **Input cell `OutPoint`s** — which cells are being consumed, linkable to the sender's lock script (address) via chain history
- **Output `CellOutput` fields** — recipient lock scripts (addresses) and capacities (CKB amounts)
- **Output cell data** — arbitrary application-layer data, including token transfer amounts for UDT transactions
- **Witnesses** — transaction signatures, which can be used to confirm the signing key

The file persists on disk after the node process exits, meaning the data remains accessible even if the node is subsequently uninstalled. This leaks the private payment activity of any user who submitted transactions via the local RPC (`send_transaction`) before the node was stopped.

### Likelihood Explanation
The default umask on most Linux distributions (`0o022`) results in the file being created with `0o644` permissions (world-readable). On a shared server or any multi-user system, this is directly exploitable by any local account. Even on single-user systems, any process running under the same UID (e.g., a compromised application, a malicious npm package, a browser extension with filesystem access) can read the file without privilege escalation. The file is written on every graceful shutdown, which is the normal operational path.

### Recommendation
**Short term:** Apply restrictive file permissions immediately after creating the persisted data file, mirroring the pattern already used for the network secret key:

```rust
#[cfg(unix)]
{
    use std::os::unix::fs::PermissionsExt;
    file.set_permissions(fs::Permissions::from_mode(0o600))?;
}
#[cfg(not(unix))]
{
    let mut permissions = file.metadata()?.permissions();
    permissions.set_readonly(false); // owner write needed for truncate on reload
    file.set_permissions(permissions)?;
}
```

**Long term:** Evaluate whether the entire `<data_dir>/tx_pool/` directory should be created with `0o700` permissions at node initialization, consistent with how sensitive key material directories are handled. Additionally, assess whether the `recent_reject` RocksDB path under the same directory warrants similar protection.

### Proof of Concept
1. Run a CKB node and submit one or more transactions via the RPC `send_transaction` method.
2. Stop the node gracefully (SIGTERM or `ckb stop`). The shutdown path triggers `save_pool()` → `save_into_file()`.
3. As a different local user (or any process with read access), read the file:
   ```
   $ cat ~/.ckb/tx_pool/persisted_data.v1 | xxd | head
   ```
4. Decode the molecule-encoded `TransactionVec` using the CKB types library:
   ```rust
   let data = std::fs::read("~/.ckb/tx_pool/persisted_data.v1").unwrap();
   let txs = TransactionVecReader::from_slice(&data).unwrap().to_entity();
   for tx in txs.into_iter() {
       let view = tx.into_view();
       println!("inputs: {:?}", view.inputs());
       println!("outputs: {:?}", view.outputs());
       println!("witnesses: {:?}", view.witnesses());
   }
   ```
5. The decoded output reveals all pending transaction details — inputs (cell references), output lock scripts (recipient addresses), capacities (amounts), and witnesses (signatures) — with no authentication required. [7](#0-6)

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

**File:** util/app-config/src/configs/network.rs (L273-284)
```rust
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                file.set_permissions(fs::Permissions::from_mode(0o400))
            }
            #[cfg(not(unix))]
            {
                let mut permissions = file.metadata()?.permissions();
                permissions.set_readonly(true);
                file.set_permissions(permissions)
            }
        })
```

**File:** util/app-config/src/configs/tx_pool.rs (L31-35)
```rust
    /// The file to persist the tx pool on the disk when tx pool have been shutdown.
    ///
    /// By default, it is a subdirectory of 'tx-pool' subdirectory under the data directory.
    #[serde(default)]
    pub persisted_data: PathBuf,
```

**File:** util/app-config/src/app_config.rs (L297-298)
```rust
        let tx_pool_path = mkdir(self.data_dir.join("tx_pool"))?;
        self.tx_pool.adjust(root_dir, tx_pool_path);
```

**File:** tx-pool/src/service.rs (L623-626)
```rust
                    _ = signal_receiver.cancelled() => {
                        info!("TxPool is saving, please wait...");
                        process_service.save_pool().await;
                        info!("TxPool process_service exit now");
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
