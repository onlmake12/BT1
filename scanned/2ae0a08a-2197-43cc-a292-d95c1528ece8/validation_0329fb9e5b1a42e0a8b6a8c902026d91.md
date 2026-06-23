### Title
Inconsistent Cycle Limit Between `_process_tx` and `readd_detached_tx` Causes Silent Transaction Loss After Reorg — (`File: tx-pool/src/process.rs`)

---

### Summary

The tx-pool uses two different cycle limits for the same script-verification operation depending on the code path. Locally submitted transactions are verified against `consensus.max_block_cycles()` (the full block-level budget), while transactions being re-added after a chain reorganization are verified against the more restrictive `tx_pool_config.max_tx_verify_cycles`. Any transaction whose actual cycle count falls between these two values is accepted on first submission but silently discarded during reorg recovery, with no error surfaced to the user.

---

### Finding Description

In `tx-pool/src/process.rs`, the function `_process_tx` selects the cycle limit for script verification as follows:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

For a locally submitted transaction (no `declared_cycles`), `max_cycles` is set to `max_block_cycles` — the consensus-level total cycle budget for an entire block (e.g., 3,500,000,000 cycles on mainnet). [1](#0-0) 

In contrast, `readd_detached_tx` — the function that re-inserts transactions into the pool after a chain reorganization — uses a completely different limit:

```rust
let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
``` [2](#0-1) 

The default value of `max_tx_verify_cycles` is `TWO_IN_TWO_OUT_CYCLES * 20` (approximately 70,000,000 cycles), which is defined in the legacy tx-pool config: [3](#0-2) 

This is roughly **50× smaller** than `max_block_cycles`. The gap between the two limits defines a range of cycle counts for which a transaction is accepted on first submission but silently dropped on reorg recovery.

The failure is silent: `readd_detached_tx` wraps `verify_rtx` in an `if let Ok(verified)` guard and simply skips the transaction if verification fails — no error is logged at a user-visible level, no rejection callback is fired, and no notification is sent to the submitter. [4](#0-3) 

For comparison, `non_contextual_verify` (the tx-pool admission gate) enforces only a size limit (`TRANSACTION_SIZE_LIMIT = 512 KB`), not a cycle limit: [5](#0-4) 

The cycle limit is enforced only inside `verify_rtx`, and the two callers pass inconsistent values.

---

### Impact Explanation

A transaction with actual cycles `C` where `max_tx_verify_cycles < C ≤ max_block_cycles`:

1. Is accepted by the tx-pool on local RPC submission (verified with `max_block_cycles`).
2. Is included in a block template and potentially mined.
3. After any chain reorganization that detaches the block containing it, `readd_detached_tx` attempts to re-add it to the pool.
4. `verify_rtx` is called with `max_tx_verify_cycles` as the limit; the VM halts early and returns an error.
5. The transaction is silently discarded. The user's transaction is permanently lost from the pool without notification.

The user must manually detect the loss and resubmit. In a high-value or time-sensitive context (e.g., a DAO withdrawal or a DeFi operation), this silent loss is a material correctness failure.

---

### Likelihood Explanation

- **Reorgs are a normal network event.** Even shallow 1–2 block reorgs occur regularly due to natural propagation races. No adversarial action is required.
- **High-cycle transactions are realistic.** Complex lock/type scripts (e.g., multi-party computation, ZK verifiers, or custom DAO scripts) can easily exceed 70M cycles while remaining well within the 3.5B block budget.
- **The entry path is fully unprivileged.** Any local RPC caller (`send_transaction`) can submit such a transaction. The reorg is triggered by the normal chain-sync process when a peer relays a longer chain.

---

### Recommendation

Unify the cycle limit used across all tx-pool verification paths. The correct authoritative limit for a single transaction in the pool is `max_tx_verify_cycles` (the per-transaction budget). `_process_tx` should use `max_tx_verify_cycles` as the fallback when `declared_cycles` is absent, not `max_block_cycles`:

```rust
// Before (inconsistent):
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());

// After (consistent):
let max_cycles = declared_cycles.unwrap_or(self.tx_pool_config.max_tx_verify_cycles);
```

Alternatively, if the intent is to allow locally submitted transactions to use the full block budget, then `readd_detached_tx` must be updated to use `max_block_cycles` as well, and the silent discard must be replaced with an explicit rejection callback so the submitter is notified.

---

### Proof of Concept

1. Configure a node with default settings (`max_tx_verify_cycles ≈ 70M`, `max_block_cycles = 3.5B`).
2. Submit a local transaction whose script consumes `C` cycles where `70M < C ≤ 3.5B`. It is accepted into the pool.
3. Mine the transaction into a block.
4. Trigger a 1-block reorg (e.g., by submitting a competing block at the same height with higher PoW).
5. Observe that `readd_detached_tx` is called for the transaction. Because `C > max_tx_verify_cycles`, `verify_rtx` returns an error, the `if let Ok` branch is skipped, and the transaction is permanently removed from the pool with no user-visible error.
6. `tx_pool_info` shows the transaction is gone; no rejection record is written. [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/process.rs (L705-750)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;

        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }

```

**File:** tx-pool/src/process.rs (L878-913)
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
```

**File:** util/app-config/src/legacy/tx_pool.rs (L14-14)
```rust
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```

**File:** tx-pool/src/util.rs (L64-73)
```rust
    // The ckb consensus does not limit the size of a single transaction,
    // but if the size of the transaction is close to the limit of the block,
    // it may cause the transaction to fail to be packed
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
```
