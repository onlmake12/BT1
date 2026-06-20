### Title
Unauthenticated `test_tx_pool_accept` RPC Endpoint Allows Unbounded CPU Exhaustion via Full-Cycle Script Verification Without Rate Limiting — (`rpc/src/module/pool.rs`, `tx-pool/src/process.rs`)

---

### Summary

The `test_tx_pool_accept` JSON-RPC endpoint performs full CKB-VM script verification up to `max_block_cycles` on every submitted transaction, with no per-caller rate limiting and no authorization check. Any unprivileged RPC caller can flood the node with crafted transactions that trigger maximum-cycle script execution, exhausting CPU and degrading node availability for all legitimate users.

---

### Finding Description

The `test_tx_pool_accept` RPC method is defined in `rpc/src/module/pool.rs` and dispatches to `TxPoolService::test_accept_tx` in `tx-pool/src/process.rs`. Unlike `submit_remote_tx` (which uses the peer-declared cycle count as the cap), `_test_accept_tx` unconditionally uses `self.consensus.max_block_cycles()` as the verification cycle limit:

```rust
// tx-pool/src/process.rs  line 787
let max_cycles = self.consensus.max_block_cycles();
```

This means every single call to `test_tx_pool_accept` may consume up to the full block cycle budget in CKB-VM execution time.

The RPC server in `rpc/src/server.rs` applies only a 30-second `TimeoutLayer` and an optional `JSONRPC_BATCH_LIMIT`. There is no per-IP connection limit, no per-method call rate limiter, and no authentication layer:

```rust
// rpc/src/server.rs  lines 119-129
let app = Router::new()
    .route("/", method_router.clone())
    ...
    .layer(CorsLayer::permissive())
    .layer(TimeoutLayer::with_status_code(
        StatusCode::REQUEST_TIMEOUT,
        Duration::from_secs(30),
    ))
    .layer(Extension(stream_config));
```

The `test_accept_tx` path does not enqueue the transaction into the verify queue or the orphan pool; it calls `_test_accept_tx` directly and synchronously runs full verification:

```rust
// tx-pool/src/process.rs  lines 386-399
pub(crate) async fn test_accept_tx(&self, tx: TransactionView) -> Result<Completed, Reject> {
    self.non_contextual_verify(&tx, None).await?;
    if self.verify_queue_contains(&tx).await { return Err(Reject::Duplicated(tx.hash())); }
    if self.orphan_contains(&tx).await { ... return Err(Reject::Duplicated(tx.hash())); }
    self._test_accept_tx(tx.clone()).await
}
```

Because the deduplication check is against the verify queue (which `_test_accept_tx` never populates), an attacker submitting distinct transactions (different inputs or witnesses) bypasses the duplicate guard entirely and triggers a fresh full-cycle verification for each call.

By contrast, the P2P relay path in `sync/src/relayer/mod.rs` does implement a rate limiter keyed by `(PeerIndex, message_type)` at 30 req/s, but this protection is absent from the RPC layer entirely.

---

### Impact Explanation

An attacker with network access to the RPC port can issue a continuous stream of `test_tx_pool_accept` calls, each referencing valid on-chain UTXOs (no ownership required — the endpoint does not consume them) and a cell dep pointing to a computationally expensive script. Each call runs CKB-VM for up to `max_block_cycles` cycles. This saturates the async Tokio thread pool used by the tx-pool service, causing:

- Legitimate `send_transaction`, `get_block_template`, and block-assembly calls to queue indefinitely.
- The node's block-proposal pipeline to stall, degrading mining throughput.
- Potential OOM pressure from concurrent verification tasks accumulating in the async runtime.

The impact is a sustained, targeted denial-of-service against the node's core transaction-processing and block-assembly subsystems.

---

### Likelihood Explanation

The RPC port is commonly exposed on public-facing nodes (e.g., infrastructure nodes, mining pools, dApps). The `test_tx_pool_accept` endpoint is part of the standard `Pool` RPC module and is enabled by default. No special privilege, key, or peer relationship is required. The attacker only needs to know valid UTXO outpoints (trivially obtained from any block explorer) and a cell dep hash for a known script. The attack requires no hashpower, no Sybil capability, and no social engineering.

---

### Recommendation

1. **Add per-IP / per-connection rate limiting** at the RPC server layer (`rpc/src/server.rs`) using a middleware such as `tower_governor` or a custom `tower::Layer`, applied specifically to computationally expensive methods (`test_tx_pool_accept`, `send_transaction`).
2. **Cap `_test_accept_tx` cycles** to `max_tx_verify_cycles` (the same cap used for async queue processing) rather than `max_block_cycles`, to bound the per-call CPU cost.
3. **Optionally gate** `test_tx_pool_accept` behind an opt-in configuration flag (disabled by default on public-facing nodes), consistent with how `clear_tx_pool` and `clear_tx_verify_queue` are treated as operator-only methods.

---

### Proof of Concept

**Entry path**: Unprivileged HTTP POST to the node's RPC port.

**Step 1** — Identify any live UTXO on-chain (e.g., from a recent block) and a cell dep pointing to a known script (e.g., the secp256k1 lock script).

**Step 2** — Craft a transaction that references the UTXO as input and includes a witness that causes the lock script to execute for many cycles (e.g., a valid-length but computationally intensive witness blob).

**Step 3** — Issue the following in a tight loop from a single client:

```bash
while true; do
  curl -s -X POST http://<node-rpc>:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"test_tx_pool_accept","params":[<crafted_tx>, "passthrough"],"id":1}' &
done
```

Because each call is distinct (different witness nonce), the `verify_queue_contains` deduplication check passes, and each call independently runs `_test_accept_tx` up to `max_block_cycles`. The node's async runtime saturates, and legitimate RPC calls begin timing out.

**Root cause lines**: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** rpc/src/server.rs (L119-129)
```rust
        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));
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

**File:** sync/src/relayer/mod.rs (L63-99)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

#[derive(Debug, Eq, PartialEq)]
pub enum ReconstructionResult {
    Block(BlockView),
    Missing(Vec<usize>, Vec<usize>),
    Collided,
    Error(Status),
}

/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
    }
```
