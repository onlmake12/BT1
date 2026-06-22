### Title
`test_tx_pool_accept` RPC Does Not Check Pool Fullness Despite Documenting `PoolIsFull` Error — (File: `tx-pool/src/process.rs`, `rpc/src/module/pool.rs`)

---

### Summary

The `test_tx_pool_accept` RPC method explicitly documents `PoolIsFull (-1106)` as a possible error return — identical to `send_transaction` — but its implementation in `_test_accept_tx` omits the pool-size-limit check (`limit_size`). As a result, the method returns success for transactions that `send_transaction` would reject with `PoolIsFull` when the pool is at capacity. This is a direct analog to the ERC4626 `maxDeposit` non-compliance: a method that should factor in a global limit (pool fullness / paused state) and signal it to callers, but silently ignores it.

---

### Finding Description

The `test_tx_pool_accept` RPC is documented as follows in `rpc/src/module/pool.rs`:

> "Test if a transaction can be accepted by the transaction pool without inserting it into the pool or rebroadcasting it to peers. The parameters and errors of this method are the same as `send_transaction`."

The listed errors include `PoolIsFull (-1106)`. [1](#0-0) 

The actual submission path `_process_tx` → `submit_entry` enforces pool fullness via `limit_size`: [2](#0-1) 

`limit_size` evicts the lowest-fee-rate transactions and, if the newly inserted transaction itself is the one evicted, returns `Reject::Full`: [3](#0-2) 

However, `_test_accept_tx` — the implementation backing `test_tx_pool_accept` — only calls `pre_check` and `verify_rtx`. It never calls `submit_entry` or `limit_size`: [4](#0-3) 

Notably, `_tx_size` is captured from `pre_check` but immediately discarded (prefixed `_`), and no pool-capacity check is performed. The `_process_tx` path that does enforce pool fullness is never invoked: [5](#0-4) 

---

### Impact Explanation

**Medium.** No funds are at risk. However, any RPC caller — wallet software, exchange integration, dApp, or script author — that uses `test_tx_pool_accept` to pre-validate a transaction before broadcasting it will receive a false positive (`Ok`) when the pool is full and the transaction's fee rate is too low to displace existing entries. The caller then submits via `send_transaction` and receives an unexpected `PoolIsFull` rejection. This breaks the contract the RPC explicitly advertises and can cause:

- Wasted user-facing retries and UX confusion.
- Incorrect fee-bumping logic in automated systems that rely on the test endpoint to decide whether to raise fees.
- Inconsistent behavior between `test_tx_pool_accept` and `send_transaction` for the same transaction under the same pool state.

---

### Likelihood Explanation

**Medium.** Pool fullness (`max_tx_pool_size`) is a routine condition during periods of high network activity. Any unprivileged RPC caller can trigger this discrepancy simply by calling `test_tx_pool_accept` when the pool is saturated. No special privileges, keys, or majority hash power are required. [6](#0-5) 

---

### Recommendation

The `_test_accept_tx` implementation should simulate the pool-capacity check. Concretely:

1. After `pre_check` succeeds, compute whether the transaction's fee rate is sufficient to survive `limit_size` eviction given the current pool state (i.e., whether `total_tx_size + tx_size > max_tx_pool_size` and the new tx has a lower fee rate than the pool's current minimum).
2. If the transaction would be evicted, return `Reject::Full(...)` — consistent with what `submit_entry` would return.
3. Alternatively, update the RPC documentation to explicitly state that `PoolIsFull` is **not** checked by `test_tx_pool_accept`, so integrators are not misled.

---

### Proof of Concept

1. Fill the tx-pool to capacity (`max_tx_pool_size`) with transactions at the minimum fee rate.
2. Craft a new transaction with a fee rate equal to or just above `min_fee_rate` but below the pool's current lowest-fee-rate entry (so it would be evicted by `limit_size`).
3. Call `test_tx_pool_accept` with this transaction → returns `Ok { cycles, fee }` (success).
4. Call `send_transaction` with the same transaction → returns `PoolIsFull (-1106)` (rejection).

The two RPC methods produce contradictory results for the same transaction under the same pool state, violating the documented contract that `test_tx_pool_accept` errors are "the same as `send_transaction`." [7](#0-6) [8](#0-7)

### Citations

**File:** rpc/src/module/pool.rs (L113-130)
```rust
    /// Test if a transaction can be accepted by the transaction pool without inserting it into the pool or rebroadcasting it to peers.
    /// The parameters and errors of this method are the same as `send_transaction`.
    ///
    /// ## Params
    ///
    /// * `transaction` - The transaction.
    /// * `outputs_validator` - Validates the transaction outputs before entering the tx-pool. (**Optional**, default is "passthrough").
    ///
    /// ## Errors
    ///
    /// * [`PoolRejectedTransactionByOutputsValidator (-1102)`](../enum.RPCError.html#variant.PoolRejectedTransactionByOutputsValidator) - The transaction is rejected by the validator specified by `outputs_validator`. If you really want to send transactions with advanced scripts, please set `outputs_validator` to "passthrough".
    /// * [`PoolRejectedTransactionByMinFeeRate (-1104)`](../enum.RPCError.html#variant.PoolRejectedTransactionByMinFeeRate) - The transaction fee rate must be greater than or equal to the config option `tx_pool.min_fee_rate`.
    /// * [`PoolRejectedTransactionByMaxAncestorsCountLimit (-1105)`](../enum.RPCError.html#variant.PoolRejectedTransactionByMaxAncestorsCountLimit) - The ancestors count must be greater than or equal to the config option `tx_pool.max_ancestors_count`.
    /// * [`PoolIsFull (-1106)`](../enum.RPCError.html#variant.PoolIsFull) - Pool is full.
    /// * [`PoolRejectedDuplicatedTransaction (-1107)`](../enum.RPCError.html#variant.PoolRejectedDuplicatedTransaction) - The transaction is already in the pool.
    /// * [`TransactionFailedToResolve (-301)`](../enum.RPCError.html#variant.TransactionFailedToResolve) - Failed to resolve the referenced cells and headers used in the transaction, as inputs or dependencies.
    /// * [`TransactionFailedToVerify (-302)`](../enum.RPCError.html#variant.TransactionFailedToVerify) - Failed to verify the transaction.
    ///
```

**File:** rpc/src/module/pool.rs (L637-660)
```rust
    fn test_tx_pool_accept(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<EntryCompleted> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();

        let test_accept_tx_reslt = tx_pool.test_accept_tx(tx).map_err(|e| {
            error!("Send test_tx_pool_accept_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })?;

        test_accept_tx_reslt
            .map(|test_accept_result| test_accept_result.into())
            .map_err(|reject| {
                error!("Send test_tx_pool_accept_tx request error {}", reject);
                RPCError::from_submit_transaction_reject(&reject)
            })
    }
```

**File:** tx-pool/src/process.rs (L150-153)
```rust
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;

```

**File:** tx-pool/src/process.rs (L386-399)
```rust
    pub(crate) async fn test_accept_tx(&self, tx: TransactionView) -> Result<Completed, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, None).await?;

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }
        self._test_accept_tx(tx.clone()).await
    }
```

**File:** tx-pool/src/process.rs (L705-777)
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

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);

        self.notify_block_assembler(status).await;

        if verify_cache.is_none() {
            // update cache
            let txs_verify_cache = Arc::clone(&self.txs_verify_cache);
            tokio::spawn(async move {
                let mut guard = txs_verify_cache.write().await;
                guard.put(wtx_hash, verified);
            });
        }

        if let Some(metrics) = ckb_metrics::handle() {
            let elapsed = instant.elapsed().as_secs_f64();
            if is_sync_process {
                metrics.ckb_tx_pool_sync_process.observe(elapsed);
            } else {
                metrics.ckb_tx_pool_async_process.observe(elapsed);
            }
        }

        Some((Ok(verified), submit_snapshot))
    }
```

**File:** tx-pool/src/process.rs (L779-800)
```rust
    pub(crate) async fn _test_accept_tx(&self, tx: TransactionView) -> Result<Completed, Reject> {
        let (pre_check_ret, snapshot) = self.pre_check(&tx).await;

        let (_tip_hash, rtx, status, _fee, _tx_size) = pre_check_ret?;

        // skip check the delay window

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = self.consensus.max_block_cycles();
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            None,
        )
        .await
    }
```

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
    }
```
