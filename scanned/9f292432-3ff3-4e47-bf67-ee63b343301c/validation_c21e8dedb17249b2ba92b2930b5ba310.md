### Title
Tx-Pool Expiry Timer Reset on Node Restart Allows Transactions to Persist Beyond Configured `expiry_hours` — (File: `tx-pool/src/persisted.rs`, `tx-pool/src/component/entry.rs`)

---

### Summary

When the CKB node shuts down, it serializes pending transactions to disk via `save_into_file()`. The serialized format stores only raw transaction bytes — no entry timestamp is preserved. On restart, transactions are reloaded and re-submitted through the normal `submit_local_tx` path, which assigns each transaction a **fresh `unix_time_as_millis()` timestamp**. This silently resets the expiry clock for every persisted transaction, allowing transactions that were near their `expiry_hours` deadline to survive for a full additional `expiry_hours` window after each restart.

---

### Finding Description

**Root cause — timestamp not persisted:**

`save_into_file()` serializes only `tx.data()` (raw molecule bytes) for each transaction:

```rust
let txs = TransactionVec::new_builder()
    .extend(self.drain_all_transactions().iter().map(|tx| tx.data()))
    .build();
``` [1](#0-0) 

No `TxEntry.timestamp` field is included in the persisted format.

**Root cause — fresh timestamp assigned on reload:**

On startup, `load_persisted_data()` calls `submit_local_tx(tx)` for each recovered transaction: [2](#0-1) 

`submit_local_tx` → `process_tx` → `_process_tx` → creates a new `TxEntry` at:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
``` [3](#0-2) 

`TxEntry::new` unconditionally stamps the entry with the current wall-clock time:

```rust
pub fn new(rtx: Arc<ResolvedTransaction>, cycles: Cycle, fee: Capacity, size: usize) -> Self {
    Self::new_with_timestamp(rtx, cycles, fee, size, unix_time_as_millis())
}
``` [4](#0-3) 

**Expiry check uses the (now-reset) timestamp:**

```rust
filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
``` [5](#0-4) 

Where `expiry = config.expiry_hours as u64 * 60 * 60 * 1000` (default 12 hours): [6](#0-5) 

Because the timestamp is reset to restart-time, a transaction submitted 11 hours before shutdown (1 hour from expiry) is treated as brand-new after restart and will not be evicted for another full 12 hours.

**Startup sequence confirming the reload path:** [7](#0-6) [8](#0-7) 

---

### Impact Explanation

An RPC caller or tx-pool submitter can submit a transaction that would normally be evicted by the `expiry_hours` policy. If the node restarts (maintenance, upgrade, crash) before the transaction expires, the expiry clock is silently reset to the restart time. With sufficiently frequent restarts, a transaction can be kept alive in the pool indefinitely without resubmission. This:

- Bypasses the operator-configured `expiry_hours` resource-management policy
- Allows stale, low-fee, or otherwise undesirable transactions to remain in the pool and be eligible for mining far beyond the intended window
- Consumes pool capacity (`max_tx_pool_size`) with transactions that should have been evicted, potentially displacing higher-fee transactions

**Impact: 2** (pool resource integrity and policy bypass; not a consensus-layer break)

---

### Likelihood Explanation

Node restarts are routine (software upgrades, crash recovery, operator maintenance). Every restart automatically resets the expiry clock for all persisted transactions with no attacker action required beyond the initial submission. The default `expiry_hours` is 12 hours; a node that restarts once every 11 hours would never expire any transaction.

**Likelihood: 3** (restarts are common; effect is automatic and requires no special attacker capability beyond submitting a transaction via RPC)

---

### Recommendation

Persist the `TxEntry.timestamp` alongside the transaction bytes in the serialized pool file. On reload, reconstruct `TxEntry` using `new_with_timestamp(…, saved_timestamp)` instead of `new(…)`. Before inserting the recovered entry, check whether `expiry + saved_timestamp < now_ms` and discard already-expired transactions rather than re-admitting them with a fresh clock.

---

### Proof of Concept

1. Configure a node with `expiry_hours = 1` (1 hour).
2. Submit a transaction via `send_transaction` RPC. Note the current time T₀.
3. Wait 55 minutes (transaction is 5 minutes from expiry).
4. Stop the node gracefully (`ckb` receives SIGTERM; `save_pool` is called).
5. Immediately restart the node.
6. Query `get_raw_tx_pool` — the transaction is present with a fresh timestamp ≈ T₀ + 55 min (restart time).
7. Wait 5 more minutes (now 60 minutes past T₀). The transaction should have expired but is not evicted.
8. Wait a full additional hour — only then does the transaction expire, demonstrating a ~55-minute expiry extension per restart cycle.

### Citations

**File:** tx-pool/src/persisted.rs (L73-75)
```rust
        let txs = TransactionVec::new_builder()
            .extend(self.drain_all_transactions().iter().map(|tx| tx.data()))
            .build();
```

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

**File:** tx-pool/src/service.rs (L732-735)
```rust
        self.started.store(true, Ordering::Release);
        if let Err(err) = self.tx_pool_controller.load_persisted_data(txs) {
            error!("Failed to import persistent txs, cause: {}", err);
        }
```

**File:** tx-pool/src/process.rs (L751-751)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

**File:** tx-pool/src/component/entry.rs (L48-50)
```rust
    pub fn new(rtx: Arc<ResolvedTransaction>, cycles: Cycle, fee: Capacity, size: usize) -> Self {
        Self::new_with_timestamp(rtx, cycles, fee, size, unix_time_as_millis())
    }
```

**File:** tx-pool/src/pool.rs (L57-57)
```rust
        let expiry = config.expiry_hours as u64 * 60 * 60 * 1000;
```

**File:** tx-pool/src/pool.rs (L277-277)
```rust
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
```
