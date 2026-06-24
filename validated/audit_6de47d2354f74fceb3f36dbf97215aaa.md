Audit Report

## Title
`max_tx_verify_cycles` Bypassed for Local RPC Submissions, Allowing ~50× CPU Overrun — (`tx-pool/src/process.rs`)

## Summary

In `TxPoolService::_process_tx`, when a transaction is submitted via the local RPC `send_transaction` endpoint, `declared_cycles` is `None`, causing the cycle budget to fall back to `consensus.max_block_cycles()` (3.5 B on mainnet) instead of the operator-configured `TxPoolConfig.max_tx_verify_cycles` (default 70 M). The configured limit is silently ignored for this entire code path, allowing any local process to force the verification thread to run scripts for up to ~50× longer than the operator intended.

## Finding Description

`TxPoolConfig.max_tx_verify_cycles` is defined at [1](#0-0)  and is correctly threaded into `VerifyQueue` for the async remote-tx path at [2](#0-1) .

However, in `_process_tx`, the cycle limit is computed as:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
``` [3](#0-2) 

For local RPC submissions, `declared_cycles` is `None`, so `max_cycles` becomes `max_block_cycles()`. This value is forwarded directly to `verify_rtx`: [4](#0-3) 

Inside `verify_rtx`, it is passed as the hard cycle ceiling to `ContextualTransactionVerifier::verify` and `verify_with_pause`: [5](#0-4)  and [6](#0-5) 

The `self.tx_pool_config.max_tx_verify_cycles` field is never read in this code path.

## Impact Explanation

**Low (501–2000 points): Important performance improvement for CKB.**

Any local process with access to the RPC port can submit transactions whose scripts consume up to 3.5 B cycles. The node executes those scripts in full before rejecting or accepting them, consuming ~50× more CPU per submission than the operator configured. Repeated submissions can saturate the verification thread pool and delay block assembly. This does not crash the node, cause consensus deviation, or affect the broader network — it is a local resource exhaustion bounded by local RPC access. The fix is a direct performance/correctness improvement ensuring the operator-configured limit is enforced.

## Likelihood Explanation

The RPC is bound to `127.0.0.1:8114` by default, so only local processes can reach this path. No credentials are required. Any script, compromised indexer, or user with shell access on the same host can trigger this repeatedly with no rate limiting beyond the verification thread pool itself.

## Recommendation

Replace the fallback in `_process_tx` with the configured limit:

```diff
- let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
+ let max_cycles = declared_cycles.unwrap_or(self.tx_pool_config.max_tx_verify_cycles);
```

This mirrors the pattern already used for the async remote-tx path via `VerifyQueue` and ensures consistent enforcement regardless of submission origin.

## Proof of Concept

1. Start a CKB node with default config (`max_tx_verify_cycles = 70_000_000`).
2. Craft a transaction whose lock script loops consuming close to `max_block_cycles` (3.5 B) cycles before returning success.
3. Submit via `send_transaction` RPC (`curl -X POST ... -d '{"method":"send_transaction",...}'`).
4. Observe via CPU profiling or timing that the verification thread runs for the full `max_block_cycles` budget, not the 70 M limit.
5. Repeat in a loop to saturate the verification thread pool and measure block assembly delay.

The root cause is confirmed at `tx-pool/src/process.rs` line 720 [3](#0-2)  and the cycle ceiling is applied without modification in `verify_rtx` at `tx-pool/src/util.rs` lines 108 and 119. [7](#0-6)

### Citations

**File:** util/app-config/src/configs/tx_pool.rs (L20-21)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
```

**File:** tx-pool/src/service.rs (L576-578)
```rust
        let verify_queue = Arc::new(RwLock::new(VerifyQueue::new(
            self.tx_pool_config.max_tx_verify_cycles,
        )));
```

**File:** tx-pool/src/process.rs (L720-720)
```rust
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

**File:** tx-pool/src/process.rs (L724-732)
```rust
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
