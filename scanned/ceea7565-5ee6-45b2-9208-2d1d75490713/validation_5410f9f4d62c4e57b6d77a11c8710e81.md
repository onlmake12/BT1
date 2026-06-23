### Title
Unbounded Repeated Full Script Verification via `test_tx_pool_accept` RPC Without Rate Limiting or Insertion Guard — (`tx-pool/src/process.rs`, `rpc/src/module/pool.rs`)

---

### Summary

The `test_tx_pool_accept` RPC endpoint triggers a full CKB-VM script execution (`ContextualTransactionVerifier`) for every call, but never inserts the transaction into the pool or the verify queue. Because the duplicate-check guard only inspects the verify queue and orphan pool — neither of which is ever populated by this path — any unprivileged RPC caller can invoke `test_tx_pool_accept` an unlimited number of times for the same transaction, each time forcing the node to execute up to `max_block_cycles` of CKB-VM computation at zero cost to the attacker.

---

### Finding Description

`test_tx_pool_accept` is a public RPC method documented as a "dry-run" that tests whether a transaction would be accepted without inserting it into the pool.

The RPC handler in `rpc/src/module/pool.rs` delegates to `tx_pool.test_accept_tx(tx)`: [1](#0-0) 

This calls `TxPoolService::test_accept_tx` in `tx-pool/src/process.rs`: [2](#0-1) 

The guard checks `verify_queue_contains` and `orphan_contains`. Both checks look for the transaction's `ProposalShortId` in the verify queue and orphan pool respectively: [3](#0-2) 

However, `_test_accept_tx` — the function that actually runs verification — **never adds the transaction to either structure**. It only calls `pre_check` and then `verify_rtx`: [4](#0-3) 

`verify_rtx` runs the full `ContextualTransactionVerifier` (CKB-VM script execution) via `block_in_place`, consuming up to `consensus.max_block_cycles()` of CPU: [5](#0-4) 

Because the transaction is never inserted into the verify queue or pool, the duplicate guard at lines 390–397 always passes for the same transaction on every subsequent call. There is no "in-progress" flag, no per-caller rate limit, and no fee charged to the RPC caller.

The verify cache (`fetch_tx_verify_cache`) is keyed by `witness_hash`. An attacker can trivially bypass it by varying the witness field across calls while keeping the same inputs and outputs, ensuring full `ContextualTransactionVerifier` execution every time.

---

### Impact Explanation

An unprivileged RPC caller can submit a crafted transaction with a script that consumes the maximum allowed cycles (`max_block_cycles`, currently 3.5 billion on mainnet) and call `test_tx_pool_accept` in a tight loop. Each call blocks a tokio worker thread (via `block_in_place`) for the full duration of script execution. With enough concurrent calls, the tx-pool service's thread pool is saturated, causing:

- Legitimate `send_transaction` submissions to stall or time out.
- Block assembly (`get_block_template`) to be delayed, harming miners.
- All other tx-pool RPC operations to queue up behind the blocked workers.

This constitutes a targeted CPU-exhaustion denial-of-service against the tx-pool service, reachable by any node with RPC access (default: localhost, but commonly exposed to local tooling or proxied services).

---

### Likelihood Explanation

The RPC endpoint is part of the standard public API, documented and enabled by default. No authentication, staking, or privileged role is required. The attacker only needs network access to the RPC port and the ability to craft a valid-looking transaction referencing a script cell that loops to the cycle limit. The attack is cheap: the attacker pays only local network/CPU cost to send HTTP requests, while the victim node bears the full CKB-VM execution cost per request.

---

### Recommendation

1. **Add an in-progress flag or a per-transaction semaphore** inside `test_accept_tx` so that concurrent or repeated calls for the same transaction are rejected until the first completes.
2. **Apply per-IP or global rate limiting** to `test_tx_pool_accept` at the RPC server layer, analogous to how `prepareSettleRaffles` was fixed with a settling flag.
3. **Cap the cycle limit** for `_test_accept_tx` to `max_tx_verify_cycles` (the per-transaction limit used in normal submission) rather than the full `max_block_cycles`.
4. Consider requiring a small fee or proof-of-work for repeated calls to this endpoint.

---

### Proof of Concept

1. Attacker deploys (or references an existing) script cell containing a tight loop that consumes exactly `max_block_cycles - 1` cycles.
2. Attacker constructs a transaction `T` spending any live cell with that script as the lock.
3. Attacker sends in a loop:
   ```
   POST /rpc  {"method":"test_tx_pool_accept","params":[T_with_varied_witness, "passthrough"]}
   ```
   varying the witness bytes each iteration to defeat the verify cache.
4. Each call enters `_test_accept_tx` → `verify_rtx` → `block_in_place(ContextualTransactionVerifier::verify(max_block_cycles))`.
5. The duplicate guard at `test_accept_tx` lines 390–397 always passes because `_test_accept_tx` never inserts `T` into the verify queue or pool.
6. The tx-pool service thread pool saturates; `send_transaction` and `get_block_template` calls from legitimate users begin timing out. [2](#0-1) [4](#0-3) [1](#0-0)

### Citations

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

**File:** tx-pool/src/process.rs (L237-245)
```rust
    pub(crate) async fn verify_queue_contains(&self, tx: &TransactionView) -> bool {
        let queue = self.verify_queue.read().await;
        queue.contains_key(&tx.proposal_short_id())
    }

    pub(crate) async fn orphan_contains(&self, tx: &TransactionView) -> bool {
        let orphan = self.orphan.read().await;
        orphan.contains_key(&tx.proposal_short_id())
    }
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
