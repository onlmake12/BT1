### Title
Inconsistent `DaoScriptSizeVerifier` Application Across Cached vs. Non-Cached Verification Paths — (`File: tx-pool/src/util.rs`)

---

### Summary

The `verify_rtx` function in `tx-pool/src/util.rs` has three branches: a cached path and two non-cached paths. The `DaoScriptSizeVerifier` check is applied only in the non-cached paths, but is entirely absent from the cached path. In contrast, the block verifier's `BlockTxsVerifier::verify` in `verification/contextual/src/contextual_block_verifier.rs` applies `DaoScriptSizeVerifier` to **both** cached and non-cached paths (gated by `rfc0044_active`). This inconsistency allows a DAO withdrawal transaction that bypasses the `DaoScriptSizeVerifier` check in the tx-pool (via the cached path) to be accepted into the pool and subsequently cause a miner's block to be rejected by the block verifier.

---

### Finding Description

**Root Cause — `tx-pool/src/util.rs`, `verify_rtx`:**

```rust
// Cached path — DaoScriptSizeVerifier is NEVER called
if let Some(completed) = cache_entry {
    TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
        .verify()
        .map(|_| *completed)
        .map_err(Reject::Verification)

// Non-cached async path — DaoScriptSizeVerifier IS called
} else if let Some(command_rx) = command_rx {
    ContextualTransactionVerifier::new(...)
        .verify_with_pause(...)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, ...).verify()?;
            Ok(result)
        })
        ...

// Non-cached sync path — DaoScriptSizeVerifier IS called
} else {
    block_in_place(|| {
        ContextualTransactionVerifier::new(...)
            .verify(...)
            .and_then(|result| {
                DaoScriptSizeVerifier::new(rtx, ...).verify()?;
                Ok(result)
            })
            ...
    })
}
``` [1](#0-0) 

**Contrast — `verification/contextual/src/contextual_block_verifier.rs`, `BlockTxsVerifier::verify`:**

The block verifier chains `.and_then()` **outside** the `if/else` so `DaoScriptSizeVerifier` runs for **both** the cached and non-cached branches when `rfc0044_active`:

```rust
if let Some(completed) = fetched_cache.get(&wtx_hash) {
    TimeRelativeTransactionVerifier::new(...).verify()...
} else {
    ContextualTransactionVerifier::new(...).verify(...)...
}.and_then(|result| {                          // ← applies to BOTH branches
    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
        DaoScriptSizeVerifier::new(Arc::clone(tx), ...).verify()?;
    }
    Ok(result)
})
``` [2](#0-1) 

**Shared cache linkage:**

The tx-pool and block verifier share the same `Arc<RwLock<TxVerificationCache>>`. The block verifier populates the cache after verifying each block's transactions. The tx-pool reads from this same cache in `fetch_tx_verify_cache` and `fetch_txs_verify_cache`. [3](#0-2) [4](#0-3) 

**Exploit path via `readd_detached_tx`:**

During a chain reorg, `update_tx_pool_for_reorg` fetches cached entries for detached-block transactions and passes them to `readd_detached_tx`, which calls `verify_rtx` with those cached entries: [5](#0-4) [6](#0-5) 

**Step-by-step exploit:**

1. Before `rfc0044` activation, an attacker (or colluding miner) crafts a DAO withdrawal transaction where the withdrawing cell uses a lock script of a **different size** than the deposit cell (deposit cell committed after `starting_block_limiting_dao_withdrawing_lock`).
2. The tx-pool's non-cached path rejects it (because `DaoScriptSizeVerifier` is always called there). The attacker bypasses the tx-pool and gets the transaction included directly in a block by a miner.
3. The block verifier accepts the block because `rfc0044_active` is `false` — `DaoScriptSizeVerifier` is not run. The transaction's `witness_hash` is written into the shared `txs_verify_cache`.
4. After `rfc0044` activation, a natural or attacker-induced chain reorg detaches that block. `readd_detached_tx` fetches the cached entry and calls `verify_rtx` with `cache_entry = Some(...)`.
5. The cached path in `verify_rtx` runs only `TimeRelativeTransactionVerifier` — `DaoScriptSizeVerifier` is skipped. The transaction is re-admitted to the tx-pool.
6. A miner assembles a new block containing this transaction. The block verifier now runs `DaoScriptSizeVerifier` (because `rfc0044_active` is `true`) and rejects the block with `DaoLockSizeMismatch`. [7](#0-6) 

---

### Impact Explanation

A miner who includes the re-admitted DAO withdrawal transaction in a block will have that block rejected by the block verifier. The miner loses the block reward (CKB coinbase + fees). If the attacker can reliably trigger reorgs or predict them, they can repeatedly cause honest miners to waste work. The inconsistency also means the tx-pool and block verifier can permanently disagree on the validity of a transaction class, violating the invariant that the tx-pool is a faithful pre-filter for block validity.

---

### Likelihood Explanation

Moderate-low. The attack requires: (a) a DAO deposit cell committed after `starting_block_limiting_dao_withdrawing_lock`, (b) a miner willing to include the malformed withdrawal transaction before `rfc0044` activation (bypassing the tx-pool), and (c) a chain reorg after `rfc0044` activation. Steps (b) and (c) are the limiting factors. However, the shared cache means the inconsistency is structurally present and will manifest whenever these conditions coincide — including naturally during the hardfork transition window.

---

### Recommendation

Apply `DaoScriptSizeVerifier` in the cached path of `verify_rtx` in `tx-pool/src/util.rs`, mirroring the block verifier's behavior. The fix should also gate the check on `rfc0044_active` (or the equivalent consensus predicate) to match the block verifier exactly:

```rust
if let Some(completed) = cache_entry {
    TimeRelativeTransactionVerifier::new(Arc::clone(&rtx), ...)
        .verify()
        .and_then(|_| {
            if consensus.rfc0044_active(...) {
                DaoScriptSizeVerifier::new(Arc::clone(&rtx), ...).verify()?;
            }
            Ok(*completed)
        })
        .map_err(Reject::Verification)
```

This ensures the tx-pool's cached path is never more permissive than the block verifier.

---

### Proof of Concept

1. Deploy a DAO deposit cell at block `B > starting_block_limiting_dao_withdrawing_lock`, using lock script `L1` (size `S1`).
2. Construct a DAO withdrawal transaction spending that deposit cell, with output lock script `L2` where `size(L2) ≠ S1`.
3. Before `rfc0044` activation: submit the transaction directly to a miner (bypassing the tx-pool). The block verifier accepts the block (`rfc0044_active = false`). The tx's `witness_hash` is cached.
4. After `rfc0044` activation: trigger or wait for a reorg that detaches the block. Observe `readd_detached_tx` calling `verify_rtx` with the cached entry — `DaoScriptSizeVerifier` is skipped, the transaction re-enters the pool.
5. Observe the next miner's block containing this transaction being rejected by `BlockTxsVerifier` with error `TransactionError::DaoLockSizeMismatch`. [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/util.rs (L85-131)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L387-456)
```rust
    pub fn verify(
        &self,
        resolved: &'a [Arc<ResolvedTransaction>],
        skip_script_verify: bool,
    ) -> Result<(Cycle, Vec<Completed>), Error> {
        // We should skip updating tx_verify_cache about the cellbase tx,
        // putting it in cache that will never be used until lru cache expires.
        let fetched_cache = if resolved.len() > 1 {
            self.fetched_cache(resolved)
        } else {
            HashMap::new()
        };

        let tx_env = Arc::new(TxVerifyEnv::new_commit(&self.header));

        // make verifiers orthogonal
        let ret = resolved
            .par_iter()
            .enumerate()
            .map(|(index, tx)| {
                let wtx_hash = tx.transaction.witness_hash();

                if let Some(completed) = fetched_cache.get(&wtx_hash) {
                    TimeRelativeTransactionVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                            Arc::clone(&tx_env),
                        )
                        .verify()
                        .map_err(|error| {
                            BlockTransactionsError {
                                index: index as u32,
                                error,
                            }
                            .into()
                        })
                        .map(|_| (wtx_hash, *completed))
                } else {
                    ContextualTransactionVerifier::new(
                        Arc::clone(tx),
                        Arc::clone(&self.context.consensus),
                        self.context.store.as_data_loader(),
                        Arc::clone(&tx_env),
                    )
                    .verify(
                        self.context.consensus.max_block_cycles(),
                        skip_script_verify,
                    )
                    .map_err(|error| {
                        BlockTransactionsError {
                            index: index as u32,
                            error,
                        }
                        .into()
                    })
                    .map(|completed| (wtx_hash, completed))
                }.and_then(|result| {
                    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
                        DaoScriptSizeVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                        ).verify()?;
                    }
                    Ok(result)
                })
            })
            .skip(1) // skip cellbase tx
            .collect::<Result<Vec<(Byte32, Completed)>, Error>>()?;
```

**File:** tx-pool/src/process.rs (L76-94)
```rust
    pub(crate) async fn fetch_tx_verify_cache(&self, tx: &TransactionView) -> Option<CacheEntry> {
        let guard = self.txs_verify_cache.read().await;
        guard.peek(&tx.witness_hash()).cloned()
    }

    async fn fetch_txs_verify_cache(
        &self,
        txs: impl Iterator<Item = &TransactionView>,
    ) -> HashMap<Byte32, CacheEntry> {
        let guard = self.txs_verify_cache.read().await;
        txs.filter_map(|tx| {
            let wtx_hash = tx.witness_hash();
            guard
                .peek(&wtx_hash)
                .cloned()
                .map(|value| (wtx_hash, value))
        })
        .collect()
    }
```

**File:** tx-pool/src/process.rs (L828-849)
```rust
        let fetched_cache = self.fetch_txs_verify_cache(retain.iter()).await;

        // If there are any transactions requires re-process, return them.
        //
        // At present, there is only one situation:
        // - If the hardfork was happened, then re-process all transactions.
        {
            // This closure is used to limit the lifetime of mutable tx_pool.
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

**File:** shared/src/shared.rs (L307-310)
```rust
    /// Returns the transaction verification cache.
    pub fn txs_verify_cache(&self) -> Arc<TokioRwLock<TxVerificationCache>> {
        Arc::clone(&self.txs_verify_cache)
    }
```

**File:** verification/src/transaction_verifier.rs (L845-886)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao_type_hash = self.dao_type_hash();
        for (i, (input_meta, cell_output)) in self
            .resolved_transaction
            .resolved_inputs
            .iter()
            .zip(self.resolved_transaction.transaction.outputs())
            .enumerate()
        {
            // Both the input and output cell must use Nervos DAO as type script
            if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
                && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
            {
                continue;
            }

            // A Nervos DAO deposit cell must have input data
            let input_data = match self.data_loader.load_cell_data(input_meta) {
                Some(data) => data,
                None => continue,
            };

            // Only input data with full zeros are counted as deposit cell
            if input_data.into_iter().any(|b| b != 0) {
                continue;
            }

            // Only cells committed after the pre-defined block number in consensus is
            // applied to this rule
            if let Some(info) = &input_meta.transaction_info
                && info.block_number
                    < self
                        .consensus
                        .starting_block_limiting_dao_withdrawing_lock()
            {
                continue;
            }

            // Now we have a pair of DAO deposit and withdrawing cells, it is expected
            // they have the lock scripts of the same size.
            if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
                return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
```
