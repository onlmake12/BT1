### Title
`max_tx_verify_cycles` Configuration Ignored for Locally Submitted Transactions — (`tx-pool/src/process.rs`)

### Summary

The `TxPoolConfig.max_tx_verify_cycles` field is explicitly configured to cap the cycle budget used when verifying individual transactions in the tx-pool. However, in `TxPoolService::_process_tx`, when a transaction is submitted locally via the RPC `send_transaction` endpoint (where `declared_cycles` is `None`), the code falls back to `consensus.max_block_cycles()` instead of the configured `max_tx_verify_cycles`. The operator-set limit is silently bypassed, and the node will run script verification with a cycle budget up to ~50× larger than intended.

---

### Finding Description

`TxPoolConfig` carries a dedicated field:

```rust
// util/app-config/src/configs/tx_pool.rs
pub max_tx_verify_cycles: Cycle,   // default 70_000_000
``` [1](#0-0) 

This value is passed into `VerifyQueue` at service startup:

```rust
// tx-pool/src/service.rs
let verify_queue = Arc::new(RwLock::new(VerifyQueue::new(
    self.tx_pool_config.max_tx_verify_cycles,
)));
``` [2](#0-1) 

So for the **async remote-tx path** (relayed transactions), `max_tx_verify_cycles` is correctly threaded through `VerifyQueue`. However, in `_process_tx` — the function that handles **both** remote and local submissions — the cycle limit is computed as:

```rust
// tx-pool/src/process.rs  line 720
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
``` [3](#0-2) 

For a **locally submitted transaction** (RPC `send_transaction`), `declared_cycles` is `None`, so `max_cycles` becomes `consensus.max_block_cycles()` — the consensus-level cap for an entire block (3 500 000 000 cycles on mainnet), not the operator-configured `max_tx_verify_cycles` (70 000 000 by default). This `max_cycles` value is then forwarded directly to `verify_rtx`:

```rust
let verified_ret = verify_rtx(
    Arc::clone(&snapshot),
    Arc::clone(&rtx),
    tx_env,
    &verify_cache,
    max_cycles,          // ← consensus.max_block_cycles(), not max_tx_verify_cycles
    command_rx,
)
.await;
``` [4](#0-3) 

Inside `verify_rtx`, this value is passed as the `max_tx_verify_cycles` argument to `ContextualTransactionVerifier::verify` / `verify_with_pause`, which in turn passes it to `TransactionScriptsVerifier::verify` as the hard cycle ceiling for CKB-VM execution:

```rust
// tx-pool/src/util.rs
pub(crate) async fn verify_rtx(
    ...
    max_tx_verify_cycles: Cycle,
    ...
) -> Result<Completed, Reject> {
    ...
    ContextualTransactionVerifier::new(...)
        .verify(max_tx_verify_cycles, false)   // ← receives max_block_cycles
``` [5](#0-4) 

The configured `max_tx_verify_cycles` stored in `self.tx_pool_config` is never read in this code path.

---

### Impact Explanation

**Impact: Medium**

An RPC caller (local CLI user or any process with access to the RPC port) can craft a transaction whose lock/type scripts consume up to `max_block_cycles` (3.5 B on mainnet) of CKB-VM cycles. The node will execute those scripts in full before rejecting or accepting the transaction, consuming ~50× more CPU time per submission than the operator intended. Repeated submissions can saturate the verification thread pool, delaying or blocking legitimate transaction processing and block assembly. The operator's explicit resource-limiting configuration (`max_tx_verify_cycles`) is rendered ineffective for the local submission path.

---

### Likelihood Explanation

**Likelihood: Low**

The RPC endpoint is bound to `127.0.0.1:8114` by default, restricting access to local processes. However, the scope explicitly includes "RPC caller" and "supported local CLI/RPC user." Any process on the same host — including a malicious script, a compromised indexer, or a user with shell access — can reach this path without any privileged credentials.

---

### Recommendation

Replace the fallback in `_process_tx` with the configured `max_tx_verify_cycles`:

```diff
- let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
+ let max_cycles = declared_cycles.unwrap_or(self.tx_pool_config.max_tx_verify_cycles);
```

This mirrors the pattern already used for the async remote-tx path via `VerifyQueue`, and ensures the operator-configured limit is consistently enforced regardless of whether the transaction originates from a peer relay or a local RPC call.

---

### Proof of Concept

1. Start a CKB node with default config (`max_tx_verify_cycles = 70_000_000`).
2. Craft a transaction whose lock script runs an infinite or very long loop, consuming close to `max_block_cycles` (3.5 B) cycles before exiting with code 0.
3. Submit via `send_transaction` RPC.
4. Observe that the node's verification thread runs the script for the full `max_block_cycles` budget (not the 70 M limit), blocking the verification thread for ~50× longer than the configured cap allows.
5. Repeat submissions to saturate the verification thread pool and delay block assembly / legitimate tx processing.

The root cause is confirmed at:
- `tx-pool/src/process.rs` line 720 — fallback uses `max_block_cycles` instead of `max_tx_verify_cycles`
- `tx-pool/src/util.rs` lines 85–131 — `verify_rtx` passes whatever `max_tx_verify_cycles` it receives directly to the CKB-VM cycle limiter [6](#0-5) [5](#0-4)

### Citations

**File:** util/app-config/src/configs/tx_pool.rs (L21-22)
```rust
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
```

**File:** tx-pool/src/service.rs (L576-578)
```rust
        let verify_queue = Arc::new(RwLock::new(VerifyQueue::new(
            self.tx_pool_config.max_tx_verify_cycles,
        )));
```

**File:** tx-pool/src/process.rs (L719-732)
```rust
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
