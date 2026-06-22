### Title
Griefing Attack via Repeated `send_transaction` + `remove_transaction` Cycle Forces Unbounded Free CKB-VM Verification Work — (File: `tx-pool/src/process.rs`, `rpc/src/module/pool.rs`)

---

### Summary

An RPC caller can repeatedly submit transactions containing expensive scripts via `send_transaction`, wait for the node to run full CKB-VM script verification, then immediately remove the transaction via `remove_transaction` before it is ever included in a block. Because the transaction fee is only realized upon block inclusion, and `remove_transaction` frees the consumed UTXOs back to live status, the attacker can cycle the same UTXOs indefinitely to exhaust node CPU resources at zero net cost. A secondary, even simpler vector exists via `test_tx_pool_accept`, which runs full verification without ever touching the pool.

---

### Finding Description

**Root cause — `send_transaction` path runs full synchronous CKB-VM verification:**

`send_transaction` dispatches `Message::SubmitLocalTx`, which calls `service.process_tx(tx, None).await`. That function calls `_process_tx`, which executes the full verification pipeline:

1. `pre_check` — cheap: resolves inputs, checks fee rate against `min_fee_rate`.
2. `verify_rtx` — **expensive**: runs CKB-VM script execution up to `max_block_cycles` (3.5 billion cycles by default).
3. `submit_entry` — adds the verified transaction to the pool. [1](#0-0) 

**Root cause — `remove_transaction` frees UTXOs with no fee collected:**

`remove_transaction` dispatches `Message::RemoveLocalTx`, which calls `service.remove_tx(tx_hash)`. This calls `pool_map.remove_entry_and_descendants`, which removes the entry and all its edge/link tracking, releasing the consumed input out-points back to live status. Because the transaction was never committed to a block, no fee is ever paid. [2](#0-1) [3](#0-2) 

**Root cause — verification cache is bypassable:**

The verification cache (`txs_verify_cache`) is keyed by `witness_hash`. An attacker who changes the witnesses between submissions (while keeping the same expensive lock/type script logic) produces a different `witness_hash` on each round, bypassing the cache and forcing a full re-execution of the CKB-VM. [4](#0-3) 

**Secondary vector — `test_tx_pool_accept` runs full verification with no pool state change at all:**

`test_tx_pool_accept` calls `service.test_accept_tx(tx)`, which runs `pre_check` and then `verify_rtx` synchronously but never calls `submit_entry`. The attacker does not even need to call `remove_transaction`; they can hammer this endpoint directly. [5](#0-4) [6](#0-5) 

**No rate limiting exists on either endpoint:**

Neither `send_transaction` nor `remove_transaction` nor `test_tx_pool_accept` has any per-caller rate limit, cooldown, or authentication beyond what the operator's network firewall provides. [7](#0-6) 

---

### Impact Explanation

- **CPU exhaustion**: Each `send_transaction` call can consume up to `max_block_cycles` of CKB-VM execution time. With multiple parallel calls, the node's async runtime is saturated.
- **Verification starvation**: The verify worker pool (`VerifyMgr`) is shared between local and remote transactions. Flooding it with attacker-controlled work delays or blocks verification of legitimate transactions.
- **Block assembly degradation**: The block assembler draws from the verified pool. If the pool is continuously drained by `remove_transaction`, miners receive templates with fewer transactions, reducing fee revenue — the direct analog to "funds left uninvested" in the original report.
- **Zero net cost to attacker**: The fee is never paid because the transaction is removed before block inclusion. The attacker only needs UTXOs large enough to satisfy `min_fee_rate` for a single transaction, which are returned after each `remove_transaction` call. [8](#0-7) 

---

### Likelihood Explanation

- The `send_transaction` and `remove_transaction` RPCs are part of the standard `PoolRpc` module, enabled by default and accessible to any local CLI/RPC user (explicitly in scope per the bounty rules).
- Operators who expose the RPC port to a LAN or the internet (common for dApp backends) extend the attack surface to remote callers.
- No authentication, no per-IP rate limit, and no minimum hold time before `remove_transaction` is permitted.
- The attack is trivially automatable with a simple loop. [9](#0-8) 

---

### Recommendation

1. **Minimum hold time**: Prevent `remove_transaction` from being called on a transaction that was submitted fewer than N seconds ago (e.g., one block interval).
2. **Rate limiting**: Apply per-IP or per-connection rate limits to `send_transaction`, `remove_transaction`, and `test_tx_pool_accept` at the RPC layer.
3. **UTXO cooldown**: After a transaction is removed via `remove_transaction`, mark its input UTXOs as temporarily unavailable for re-submission for a short window, breaking the free cycling loop.
4. **Restrict `test_tx_pool_accept`**: Move it behind an authenticated or localhost-only RPC group, or add a per-call cycle budget that is charged against a caller quota.

---

### Proof of Concept

```
# Attacker setup: one UTXO with capacity >= min_fee_rate * tx_size

loop:
  # Step 1: craft tx with expensive lock script (e.g., tight RISC-V loop
  #         consuming ~max_block_cycles), change witness bytes each iteration
  #         to produce a fresh witness_hash and bypass txs_verify_cache.
  tx = build_tx(inputs=[attacker_utxo], expensive_script=True, witness=nonce++)

  # Step 2: submit — node runs full CKB-VM verification (seconds of CPU)
  hash = rpc.send_transaction(tx)          # process_tx → _process_tx → verify_rtx

  # Step 3: remove — UTXOs freed, no fee paid, pool drained
  rpc.remove_transaction(hash)             # remove_entry_and_descendants

  # attacker_utxo is live again; repeat immediately
```

Each iteration burns up to `max_block_cycles` of node CPU at zero cost to the attacker, continuously draining the verified pool and starving legitimate transaction throughput — the direct CKB analog of the deposit/withdrawal griefing attack described in the reference report. [10](#0-9) [2](#0-1) [11](#0-10)

### Citations

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

**File:** tx-pool/src/process.rs (L780-800)
```rust
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

**File:** tx-pool/src/pool.rs (L358-361)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
    }
```

**File:** rpc/src/module/pool.rs (L606-669)
```rust
impl PoolRpc for PoolRpcImpl {
    fn tx_pool_ready(&self) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();
        Ok(tx_pool.service_started())
    }

    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();
        let submit_tx = tx_pool.submit_local_tx(tx.clone());

        if let Err(e) = submit_tx {
            error!("Send submit_tx request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        }

        let tx_hash = tx.hash();
        match submit_tx.unwrap() {
            Ok(_) => Ok(tx_hash.into()),
            Err(reject) => Err(RPCError::from_submit_transaction_reject(&reject)),
        }
    }

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

    fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();

        tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
            error!("Send remove_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })
    }
```

**File:** tx-pool/src/verify_mgr.rs (L109-163)
```rust
    async fn process_inner(&mut self) {
        loop {
            if self.exit_signal.is_cancelled() {
                info!("Verify worker::process_inner exit_signal is cancelled");
                return;
            }
            self.refresh_status();
            if self.status != ChunkCommand::Resume {
                return;
            }
            // cheap query to check queue is not empty
            if self.tasks.read().await.is_empty() {
                return;
            }

            self.refresh_status();
            if self.status != ChunkCommand::Resume {
                return;
            }

            // pick a entry to run verify
            let entry = {
                let mut tasks = self.tasks.write().await;
                match tasks.pop_front(self.role == WorkerRole::OnlySmallCycleTx) {
                    Some(entry) => entry,
                    None => {
                        if !tasks.is_empty() {
                            tasks.re_notify();
                            debug!(
                                "Worker (role: {:?}) didn't got tx after pop_front, but tasks is not empty, notify other Workers now",
                                self.role
                            );
                        }
                        return;
                    }
                }
            };

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
    }
```

**File:** tx-pool/src/service.rs (L805-834)
```rust
        Message::SubmitLocalTx(Request {
            responder,
            arguments: tx,
        }) => {
            let result = service.process_tx(tx, None).await.map(|_| ());
            if let Err(e) = responder.send(result) {
                error!("Responder sending submit_tx result failed {:?}", e);
            };
        }
        Message::SubmitLocalTestTx(Request {
            responder,
            arguments: tx,
        }) => {
            let result = service
                .resumeble_process_tx(tx, false, None)
                .await
                .map(|_| ());
            if let Err(e) = responder.send(result) {
                error!("Responder sending submit_tx result failed {:?}", e);
            };
        }
        Message::RemoveLocalTx(Request {
            responder,
            arguments: tx_hash,
        }) => {
            let result = service.remove_tx(tx_hash).await;
            if let Err(e) = responder.send(result) {
                error!("Responder sending remove_tx result failed {:?}", e);
            };
        }
```
