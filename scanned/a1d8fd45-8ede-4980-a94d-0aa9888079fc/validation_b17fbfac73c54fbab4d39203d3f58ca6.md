## Analysis

Let me trace the full call path and verify each claim against the actual code.

### Call Path Verification

**Step 1: RPC entry point**

`PoolRpcImpl::test_tx_pool_accept` calls `tx_pool.test_accept_tx(tx)` with no rate limiting, no authentication, and no per-caller throttle. [1](#0-0) 

**Step 2: Controller dispatch**

`TxPoolController::test_accept_tx` uses the `send_message!` macro, which does a `try_send` into a bounded channel (capacity `DEFAULT_CHANNEL_SIZE = 512`) and then blocks the calling thread with `block_in_place(|| response.recv())`. [2](#0-1) [3](#0-2) 

**Step 3: Service loop — each message is spawned as an independent tokio task**

This is the architectural crux. The service loop does **not** process messages sequentially in a single actor; it spawns each message as a separate concurrent task:

```rust
Some(message) = receiver.recv() => {
    let service_clone = process_service.clone();
    handle_clone.spawn(process(service_clone, message));
},
``` [4](#0-3) 

**Step 4: `Message::TestAcceptTx` handler** [5](#0-4) 

**Step 5: `TxPoolService::test_accept_tx` — no deduplication for test-only calls**

It checks the verify queue and orphan pool, but since `_test_accept_tx` never inserts the tx anywhere, the same transaction can be submitted again immediately after the call returns. [6](#0-5) 

**Step 6: `_test_accept_tx` — the root cause**

```rust
let max_cycles = self.consensus.max_block_cycles();
...
verify_rtx(
    Arc::clone(&snapshot),
    Arc::clone(&rtx),
    tx_env,
    &verify_cache,
    max_cycles,
    None,   // ← command_rx = None: no chunking, no suspension
)
.await
``` [7](#0-6) 

Compare this to the normal `_process_tx` path, which passes a real `command_rx` from `VerifyMgr` so that block arrival can suspend in-progress verification: [8](#0-7) 

### Why This Matters

| Property | `_process_tx` (normal submit) | `_test_accept_tx` |
|---|---|---|
| `max_cycles` | `declared_cycles` or `max_block_cycles()` | Always `max_block_cycles()` |
| `command_rx` | Passed from `VerifyMgr` (interruptible) | `None` (runs to completion) |
| Deduplication | Rejected as `Duplicated` on second submit | No pool insertion → no dedup |
| Rate limiting | None | None |

Because `command_rx = None`, the CKB-VM verification runs to completion with no yield point and no way for the runtime to suspend it when a new block arrives. Because the service loop spawns each message as a separate tokio task, N concurrent RPC callers produce N concurrent unbounded CPU-intensive verifications. Because `_test_accept_tx` never inserts the transaction, the same max-cycle transaction can be resubmitted immediately after each response.

### Limiting Factors

- The channel is bounded at 512 (`DEFAULT_CHANNEL_SIZE`), so at most 512 requests can be queued. However, since each is spawned immediately as a task, the actual concurrency is bounded by the tokio thread pool (typically = CPU core count), which is exactly what the attacker wants to saturate.
- The RPC is bound to `127.0.0.1` by default. Nodes that expose RPC to the network (mining pools, public nodes) are directly reachable.
- No PoW, no key, no privileged role required — just a valid transaction referencing live cells.

---

### Title
Unbounded CPU Exhaustion via Repeated `test_tx_pool_accept` with Max-Cycle Script — (`tx-pool/src/process.rs`, `rpc/src/module/pool.rs`)

### Summary
An unprivileged caller with access to the Pool RPC can repeatedly invoke `test_tx_pool_accept` with a transaction whose lock script consumes `consensus.max_block_cycles()` cycles. Each call runs `verify_rtx` with `command_rx = None` (no chunking, no suspension) inside a freshly spawned tokio task. Because the transaction is never inserted into the pool, there is no deduplication guard, and the same call can be repeated indefinitely. Concurrent flooding saturates the tokio thread pool with CPU-bound work, starving block template generation and relay processing.

### Finding Description
`_test_accept_tx` hardcodes `max_cycles = self.consensus.max_block_cycles()` and passes `command_rx = None` to `verify_rtx`. The normal submission path (`_process_tx`) passes a real `command_rx` from `VerifyMgr` so that block arrival can interrupt in-progress verification. `_test_accept_tx` has no such mechanism. The service loop spawns each `Message::TestAcceptTx` as an independent tokio task, so N concurrent RPC callers produce N concurrent uninterruptible max-cycle verifications. No rate limiting exists at the RPC layer.

### Impact Explanation
- Sustained CPU saturation across all tokio worker threads.
- `get_block_template` requests are spawned as tasks but cannot be scheduled while worker threads are pinned by `verify_rtx`.
- Relay and reorg processing share the same tokio runtime and are similarly starved.
- Block template generation stalls, causing the node to miss block intervals — constituting low-cost network congestion.

### Likelihood Explanation
Any node with the Pool RPC module enabled and the RPC endpoint reachable (common for mining pool backends, public RPC nodes) is exploitable. The attacker needs only one valid UTXO to construct the transaction. The same transaction can be reused across all calls. No hashpower, no key material, no privileged access required.

### Recommendation
1. Pass a real `command_rx` to `verify_rtx` inside `_test_accept_tx` so that block arrival can interrupt verification, consistent with the normal submission path.
2. Apply a per-IP or global concurrency limit on `test_tx_pool_accept` at the RPC layer.
3. Cap `max_cycles` for `test_tx_pool_accept` to a fraction of `max_block_cycles()` (e.g., `max_tx_verify_cycles`).

### Proof of Concept
1. Deploy a CKB devnet node with `[rpc] modules = ["Pool"]`.
2. Deploy a lock script that loops for `max_block_cycles` cycles.
3. Create a transaction spending a cell locked by that script.
4. Spawn 16 threads each calling `test_tx_pool_accept` with that transaction in a tight loop.
5. Concurrently call `get_block_template` and measure response latency.
6. Assert that `get_block_template` latency exceeds one block interval (8 seconds on mainnet).

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

**File:** tx-pool/src/service.rs (L171-186)
```rust
macro_rules! send_message {
    ($self:ident, $msg_type:ident, $args:expr) => {{
        let (responder, response) = oneshot::channel();
        let request = Request::call($args, responder);
        $self
            .sender
            .try_send(Message::$msg_type(request))
            .map_err(|e| {
                let (_m, e) = handle_try_send_error(e);
                e
            })?;
        block_in_place(|| response.recv())
            .map_err(handle_recv_error)
            .map_err(Into::into)
    }};
}
```

**File:** tx-pool/src/service.rs (L265-270)
```rust
    /// test if a tx can be accepted by tx-pool
    /// Won't be broadcasted to network
    /// won't be insert to tx-pool
    pub fn test_accept_tx(&self, tx: TransactionView) -> Result<TestAcceptTxResult, AnyError> {
        send_message!(self, TestAcceptTx, tx)
    }
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

**File:** tx-pool/src/service.rs (L835-843)
```rust
        Message::TestAcceptTx(Request {
            responder,
            arguments: tx,
        }) => {
            let result = service.test_accept_tx(tx).await;
            if let Err(e) = responder.send(result.map(|r| r.into())) {
                error!("Responder sending test_accept_tx result failed {:?}", e);
            };
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

**File:** tx-pool/src/process.rs (L705-732)
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
