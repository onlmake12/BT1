### Title
Tx-Pool Re-verifies All Persisted Transactions on Every Startup Without Caching Verification Results - (File: `tx-pool/src/service.rs`)

---

### Summary

On every startup, the CKB tx-pool loads its persisted raw transactions from disk and re-submits each one through the full verification pipeline (including CKB-VM script execution) via `load_persisted_data` → `submit_local_tx`. Verification results are never cached to disk. An unprivileged attacker who fills the tx-pool with transactions near the maximum cycle limit can cause the node to spend significant CPU time re-verifying the entire pool on every restart, delaying block template availability and consuming node resources.

---

### Finding Description

When `TxPoolServiceBuilder::start` is called, it loads raw transactions from the persisted file and then calls `load_persisted_data`, which iterates over every transaction and calls `submit_local_tx` for each one: [1](#0-0) [2](#0-1) 

`load_persisted_data` calls `submit_local_tx` for every persisted transaction: [3](#0-2) 

`submit_local_tx` routes to `process_tx`, which performs full script execution via CKB-VM. The persisted file (`save_into_file`) stores only raw transaction bytes — no verification results, no cycle counts, no cached validity: [4](#0-3) 

This means every restart unconditionally re-executes all scripts for all persisted transactions, regardless of how many times the node has already verified them.

---

### Impact Explanation

The total re-verification work on startup is bounded by:

```
(number of pool transactions) × (max_tx_verify_cycles per tx) × (time per cycle)
```

There is no per-pool-total cycle cap. With `max_tx_pool_size` defaulting to a large byte budget and `max_tx_verify_cycles` allowing expensive scripts per transaction, an attacker can fill the pool with many small but cycle-heavy transactions. On every restart, the node must re-execute all of them before the tx-pool is fully operational.

During this period:
- Block template generation is delayed or returns incomplete templates (missing pool transactions that haven't been re-loaded yet)
- Node CPU is saturated by script re-execution, degrading P2P and RPC responsiveness
- Miners connected to the node receive suboptimal or stale block templates

---

### Likelihood Explanation

The attack requires only the ability to call `send_transaction` RPC — available to any unprivileged user. The attacker submits many transactions with scripts that consume close to `max_tx_verify_cycles` cycles each, filling the pool. The node operator will eventually restart the node (routine maintenance, upgrade, crash recovery). Each restart triggers the full re-verification. The attacker does not need to be present at restart time; the pool state persists across graceful shutdowns. [5](#0-4) 

---

### Recommendation

**Short term**: Persist verification results (e.g., verified cycle counts) alongside raw transaction bytes in the persisted data file. On reload, skip re-execution for transactions whose cached cycle count is present and whose inputs are still live. Validate the cached state against the current chain snapshot before trusting it.

**Long term**: Add a startup benchmark test that measures tx-pool reload time with a pool filled to capacity with max-cycle transactions, and enforce a time bound. Consider a configurable `max_pool_reload_cycles` cap to bound worst-case startup cost.

---

### Proof of Concept

1. Connect to a CKB node with RPC access.
2. Submit N transactions, each with a lock script that consumes close to `max_tx_verify_cycles` cycles (e.g., a tight loop in CKB-VM). Ensure total byte size stays under `max_tx_pool_size`.
3. Wait for the node to perform a graceful shutdown (or trigger one via `ckb stop`). The pool is saved via `save_into_file`.
4. Restart the node. Observe that `TxPoolServiceBuilder::start` calls `load_persisted_data`, which calls `submit_local_tx` → `process_tx` → full CKB-VM execution for every transaction.
5. Measure the time before the tx-pool is fully loaded and block templates include all pool transactions. With a large pool of max-cycle transactions, this delay is proportional to N × max_tx_verify_cycles.

The root cause is in:
- `tx-pool/src/service.rs` lines 580–588 and 733–735 (`TxPoolServiceBuilder::start`)
- `tx-pool/src/service.rs` lines 433–453 (`load_persisted_data`)
- `tx-pool/src/persisted.rs` lines 57–90 (`save_into_file` — no cycle/validity data saved) [4](#0-3) [3](#0-2) [1](#0-0) [6](#0-5)

### Citations

**File:** tx-pool/src/service.rs (L433-453)
```rust
    /// Load persisted txs into pool, assume that all txs are sorted
    fn load_persisted_data(&self, txs: Vec<TransactionView>) -> Result<(), AnyError> {
        if !txs.is_empty() {
            info!("Loading persistent tx-pool data, total {} txs", txs.len());
            let mut failed_txs = 0;
            for tx in txs {
                if self.submit_local_tx(tx)?.is_err() {
                    failed_txs += 1;
                }
            }
            if failed_txs == 0 {
                info!("Persistent tx-pool data is loaded");
            } else {
                info!(
                    "Persistent tx-pool data is loaded, {} stale txs are ignored",
                    failed_txs
                );
            }
        }
        Ok(())
    }
```

**File:** tx-pool/src/service.rs (L580-588)
```rust
        let tx_pool = TxPool::new(self.tx_pool_config, self.snapshot);
        let txs = match tx_pool.load_from_file() {
            Ok(txs) => txs,
            Err(e) => {
                error!("{}", e.to_string());
                error!("Failed to load txs from tx-pool persistent data file, all txs are ignored");
                Vec::new()
            }
        };
```

**File:** tx-pool/src/service.rs (L616-632)
```rust
        self.handle.spawn(async move {
            loop {
                tokio::select! {
                    Some(message) = receiver.recv() => {
                        let service_clone = process_service.clone();
                        handle_clone.spawn(process(service_clone, message));
                    },
                    _ = signal_receiver.cancelled() => {
                        info!("TxPool is saving, please wait...");
                        process_service.save_pool().await;
                        info!("TxPool process_service exit now");
                        break
                    },
                    else => break,
                }
            }
        });
```

**File:** tx-pool/src/service.rs (L732-735)
```rust
        self.started.store(true, Ordering::Release);
        if let Err(err) = self.tx_pool_controller.load_persisted_data(txs) {
            error!("Failed to import persistent txs, cause: {}", err);
        }
```

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
