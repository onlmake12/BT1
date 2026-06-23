The code path is concrete and verifiable. Let me trace each step precisely.

**Step 1: `update_tx_pool_for_reorg` holds the write lock for the entire reorg**

In `process.rs` lines 836–851, the write lock is acquired and held across the entire `readd_detached_tx` call:

```rust
let mut tx_pool = self.tx_pool.write().await;   // lock acquired
_update_tx_pool_for_reorg(...);
self.readd_detached_tx(&mut tx_pool, retain, fetched_cache).await;  // lock still held
// } <- lock released only here
``` [1](#0-0) 

**Step 2: `readd_detached_tx` calls `verify_rtx` with `command_rx=None` for every detached tx** [2](#0-1) 

The `None` is hardcoded at line 901.

**Step 3: `verify_rtx` with `command_rx=None` and no cache hit calls `block_in_place`** [3](#0-2) 

`block_in_place` runs `ContextualTransactionVerifier::verify` synchronously on the current thread — the async task does not yield, and the write lock remains held for the full duration of script execution.

**Step 4: Verify workers need the write lock via `submit_entry`**

Workers in `verify_mgr.rs` call `_process_tx` → `submit_entry` → `with_tx_pool_write_lock`. They are blocked for the entire reorg duration. [4](#0-3) [5](#0-4) 

**Attacker path requirement:** Triggering a reorg requires a valid block with valid PoW. This is not zero-cost, but it does not require majority hashpower — any miner (even a small one) can occasionally produce a competing block. The attacker mines a block full of high-cycle transactions (up to `max_tx_verify_cycles` each), causing a 1-block reorg. The scope rules reject attacks requiring "malicious majority hashpower," but a 1-block reorg does not require that.

---

### Title
Tx-pool write lock held during synchronous script verification in `readd_detached_tx`, causing complete tx-pool stall on reorg — (`tx-pool/src/process.rs`, `tx-pool/src/util.rs`)

### Summary
`update_tx_pool_for_reorg` acquires the `tx_pool` write lock and holds it while calling `readd_detached_tx`, which in turn calls `verify_rtx(..., None)` for each detached transaction. With `command_rx=None` and no cache hit, `verify_rtx` falls into `block_in_place(ContextualTransactionVerifier::verify(...))` — a fully synchronous, non-yielding script execution. The write lock is never released between transactions. All verify workers (which need the write lock to call `submit_entry`) and all RPC handlers (which need the read lock) are blocked for the entire duration.

### Finding Description
In `tx-pool/src/process.rs`, `update_tx_pool_for_reorg` wraps both `_update_tx_pool_for_reorg` and `readd_detached_tx` inside a single `tx_pool.write().await` scope (lines 836–851). Inside `readd_detached_tx` (lines 878–914), for each detached transaction that is not in the verification cache, `verify_rtx` is called with `command_rx = None`. In `tx-pool/src/util.rs` lines 116–130, the `None` branch calls `tokio::task::block_in_place(|| ContextualTransactionVerifier::new(...).verify(max_tx_verify_cycles, false))`. This is a synchronous blocking call: the async task does not yield, the Tokio runtime thread is occupied, and the `RwLock` write guard remains live. For K detached transactions each consuming up to `max_tx_verify_cycles` cycles, the write lock is held for O(K × max_tx_verify_cycles) script execution time.

### Impact Explanation
- **Verify workers fully stalled**: Every worker in `verify_mgr.rs` eventually calls `submit_entry` → `with_tx_pool_write_lock`. Since the write lock is held, all workers block. No new transactions are admitted to the pool.
- **All read-lock RPC paths blocked**: RPCs such as `get_tx_pool_info`, `get_transaction`, `fetch_txs`, `fetch_txs_with_cycles` all call `tx_pool.read().await`. Tokio's `RwLock` does not allow new readers while a writer is waiting or active, so these RPCs hang for the full duration.
- **Duration scales with attacker-controlled K**: The attacker fills a competing block with K transactions each at `max_tx_verify_cycles`. A single 1-block reorg with a full block of max-cycle transactions can stall the tx-pool for seconds to tens of seconds.

### Likelihood Explanation
The attacker must mine a valid block (PoW required), but does not need majority hashpower. Any miner — even one with a small fraction of hashrate — can occasionally produce a competing block at the same height. The attacker pre-populates the block with transactions crafted to maximize script execution time. This is a low-cost, repeatable attack for any participant with mining capability.

### Recommendation
Release the write lock before script verification in `readd_detached_tx`. The function should:
1. Collect all `(rtx, status, fee, tx_size, tx_env, cache)` tuples while holding the read lock (or no lock).
2. Run all `verify_rtx` calls outside any lock.
3. Re-acquire the write lock only to call `_submit_entry` for each verified entry.

This mirrors the pattern already used in the normal tx submission path (`_process_tx`), where `verify_rtx` is called before `submit_entry`.

### Proof of Concept
1. Craft K transactions each consuming `max_tx_verify_cycles` cycles (e.g., tight loops in CKB-VM scripts).
2. Mine a valid competing block at height H containing these K transactions (requires PoW, not majority hashrate).
3. Relay the competing block to a target node that already has a block at height H (triggering a 1-block reorg).
4. The target node calls `update_tx_pool_for_reorg` → acquires write lock → `readd_detached_tx` → K × `block_in_place(verify)`.
5. Measure: all `submit_entry` calls from verify workers block; all `tx_pool.read()` RPC calls block; tx-pool is completely unresponsive for the duration proportional to K × cycles.

### Citations

**File:** tx-pool/src/process.rs (L96-103)
```rust
    pub(crate) async fn submit_entry(
        &self,
        pre_resolve_tip: Byte32,
        entry: TxEntry,
        mut status: TxStatus,
    ) -> (Result<(), Reject>, Arc<Snapshot>) {
        let (ret, snapshot) = self
            .with_tx_pool_write_lock(move |tx_pool, snapshot| {
```

**File:** tx-pool/src/process.rs (L836-851)
```rust
            let mut tx_pool = self.tx_pool.write().await;

            _update_tx_pool_for_reorg(
                &mut tx_pool,
                &attached,
                &detached_headers,
                detached_proposal_id,
                snapshot,
                &self.callbacks,
                mine_mode,
            );

            // notice: readd_detached_tx don't update cache
            self.readd_detached_tx(&mut tx_pool, retain, fetched_cache)
                .await;
        }
```

**File:** tx-pool/src/process.rs (L878-914)
```rust
    async fn readd_detached_tx(
        &self,
        tx_pool: &mut TxPool,
        txs: Vec<TransactionView>,
        fetched_cache: HashMap<Byte32, CacheEntry>,
    ) {
        let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
        for tx in txs {
            let tx_size = tx.data().serialized_size_in_block();
            let tx_hash = tx.hash();
            if let Ok((rtx, status)) = resolve_tx(tx_pool, tx_pool.snapshot(), tx, false)
                && let Ok(fee) = check_tx_fee(tx_pool, tx_pool.snapshot(), &rtx, tx_size)
            {
                let verify_cache = fetched_cache.get(&tx_hash).cloned();
                let snapshot = tx_pool.cloned_snapshot();
                let tip_header = snapshot.tip_header();
                let tx_env = Arc::new(status.with_env(tip_header));
                if let Ok(verified) = verify_rtx(
                    snapshot,
                    Arc::clone(&rtx),
                    tx_env,
                    &verify_cache,
                    max_cycles,
                    None,
                )
                .await
                {
                    let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
                    if let Err(e) = _submit_entry(tx_pool, status, entry, &self.callbacks) {
                        error!("readd_detached_tx submit_entry {} error {}", tx_hash, e);
                    } else {
                        debug!("readd_detached_tx submit_entry {}", tx_hash);
                    }
                }
            }
        }
    }
```

**File:** tx-pool/src/util.rs (L116-131)
```rust
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
```

**File:** tx-pool/src/verify_mgr.rs (L147-162)
```rust
            if let Some((res, snapshot)) = self
                .service
                ._process_tx(
                    entry.tx.clone(),
                    entry.remote.map(|e| e.0),
                    Some(&mut self.command_rx),
                )
                .await
            {
                self.service
                    .after_process(entry.tx, entry.remote, &snapshot, &res)
                    .await;
            } else {
                info!("_process_tx for tx: {} returned none", entry.tx.hash());
            }
        }
```
