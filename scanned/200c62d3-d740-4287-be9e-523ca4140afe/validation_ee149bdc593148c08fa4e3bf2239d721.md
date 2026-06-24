Audit Report

## Title
No Rate Limiting on `estimate_cycles` RPC Allows Unbounded CKB-VM Execution — (File: `rpc/src/module/chain.rs`)

## Summary
The `estimate_cycles` RPC method (and its deprecated alias `dry_run_transaction`) accepts an attacker-supplied transaction and synchronously executes the full CKB-VM script verifier up to `consensus.max_block_cycles` cycles per call, with no rate limiting, no concurrency cap, and no per-caller throttle at the RPC server layer. An unprivileged caller who can reach the RPC port can flood the node with concurrent requests, each consuming up to the maximum block cycle budget of CPU time on the Tokio thread pool, starving the node of resources needed for block processing, P2P relay, and tx-pool operation, effectively crashing the node's consensus-critical functions.

## Finding Description
`estimate_cycles` is implemented in `ChainRpcImpl` as a direct, unguarded delegation to `CyclesEstimator::run`:

```rust
fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles> {
    let tx: packed::Transaction = tx.into();
    CyclesEstimator::new(&self.shared).run(tx)
}
``` [1](#0-0) 

Inside `CyclesEstimator::run`, the cycle cap is set to `consensus.max_block_cycles` — the maximum cycle budget for an entire block — and `ScriptVerifier::verify` is called synchronously with no `spawn_blocking` or `block_in_place` wrapper:

```rust
let max_cycles = consensus.max_block_cycles;
...
ScriptVerifier::new(Arc::new(resolved), snapshot.as_data_loader(), consensus, Arc::new(tx_env))
    .verify(max_cycles)
``` [2](#0-1) 

This is a blocking CPU-bound operation executing directly on the Tokio async runtime thread pool. No `spawn_blocking` is used anywhere in `rpc/src/` for this path, confirmed by the absence of any such call. [3](#0-2) 

The RPC server applies only a 30-second HTTP timeout and an opt-in batch size limit (`JSONRPC_BATCH_LIMIT`) that is **disabled by default** (commented out in the default config):

```toml
# By default, there is no limitation on the size of batch request size
# rpc_batch_limit = 2000
``` [4](#0-3) 

The server layer adds no per-IP rate limit, no per-method concurrency cap, and no global request throttle: [5](#0-4) 

A grep for `rate.limit`, `RateLimiter`, `semaphore`, `concurrency.cap`, or `throttle` in `rpc/src/**` returns zero matches, confirming the complete absence of any such guard.

The deprecated `dry_run_transaction` in the Experiment module is identical — it calls the same `CyclesEstimator::run` with no additional guard: [6](#0-5) 

Notably, the tx-pool config already defines a much lower `max_tx_verify_cycles = 70_000_000` for transaction admission, but `estimate_cycles` ignores this and uses the full `max_block_cycles` instead: [7](#0-6) 

## Impact Explanation
Each `estimate_cycles` call runs the CKB-VM synchronously on the Tokio thread pool for up to `max_block_cycles` cycles. Because `ScriptVerifier::verify` is not wrapped in `spawn_blocking`, it occupies a Tokio worker thread for the full duration of VM execution. N concurrent requests force N simultaneous blocking VM executions on the async runtime, starving all other async tasks — including block template assembly, compact block relay, transaction pool admission, and peer-to-peer sync — of CPU and thread availability. Under sustained saturation, the node becomes unable to process blocks or maintain peer connections, constituting a crash of the node's consensus-critical functions.

This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
The RPC endpoint is reachable by any caller who can reach the node's RPC port. While the default bind address is `127.0.0.1:8114`, many operators expose the RPC publicly for dApp backends, block explorers, or wallet services. No authentication, no API key, and no proof-of-work is required. The attack requires only the ability to send HTTP POST requests and knowledge of any live cell on-chain (trivially obtained from any block explorer) to construct a valid resolvable transaction with a maximally-looping script. The 30-second timeout per request means an attacker can sustain continuous CPU pressure with a modest number of concurrent connections (e.g., 64 threads). The attack is repeatable, requires no privileged access, and has negligible cost.

## Recommendation
1. **Add a per-method concurrency semaphore** for CPU-intensive RPC calls (`estimate_cycles`, `dry_run_transaction`) at the RPC handler layer, limiting simultaneous VM executions to a small fixed number (e.g., 4).
2. **Wrap `ScriptVerifier::verify` in `spawn_blocking`** so that VM execution does not block Tokio worker threads.
3. **Cap `max_cycles` for RPC estimation** to `max_tx_verify_cycles` from the tx-pool config (currently 70,000,000) rather than `consensus.max_block_cycles`.
4. **Enable `rpc_batch_limit` by default** and document the CPU-exhaustion risk of `estimate_cycles` in the RPC reference.

## Proof of Concept
```python
import requests, threading

# Construct a transaction referencing a live cell with a maximally-looping lock script.
payload = {
    "jsonrpc": "2.0", "method": "estimate_cycles", "id": 1,
    "params": [{ "cell_deps": [...], "inputs": [...], "outputs": [...],
                 "outputs_data": ["0x"], "version": "0x0", "witnesses": [] }]
}

def flood():
    while True:
        requests.post("http://<node>:8114/", json=payload, timeout=35)

# Launch N concurrent threads — each holds a VM execution for up to max_block_cycles
threads = [threading.Thread(target=flood) for _ in range(64)]
for t in threads: t.start()
# Node's block processing and P2P relay degrade under sustained CPU saturation
```

The attacker needs one valid live cell reference on-chain and a script that loops to the cycle limit. No privileged access, no key material, and no majority hashpower is required. The node's responsiveness to block relay and peer sync can be monitored externally to confirm degradation.

### Citations

**File:** rpc/src/module/chain.rs (L2119-2122)
```rust
    fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles> {
        let tx: packed::Transaction = tx.into();
        CyclesEstimator::new(&self.shared).run(tx)
    }
```

**File:** rpc/src/module/chain.rs (L2375-2399)
```rust
    pub(crate) fn run(&self, tx: packed::Transaction) -> Result<EstimateCycles> {
        let snapshot = self.shared.cloned_snapshot();
        let consensus = snapshot.cloned_consensus();
        match resolve_transaction(tx.into_view(), &mut HashSet::new(), self, self) {
            Ok(resolved) => {
                let max_cycles = consensus.max_block_cycles;
                let tip_header = snapshot.tip_header();
                let tx_env = TxVerifyEnv::new_submit(tip_header);
                match ScriptVerifier::new(
                    Arc::new(resolved),
                    snapshot.as_data_loader(),
                    consensus,
                    Arc::new(tx_env),
                )
                .verify(max_cycles)
                {
                    Ok(cycles) => Ok(EstimateCycles {
                        cycles: cycles.into(),
                    }),
                    Err(err) => Err(RPCError::custom_with_error(
                        RPCError::TransactionFailedToVerify,
                        err,
                    )),
                }
            }
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
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

**File:** rpc/src/module/experiment.rs (L230-233)
```rust
    fn dry_run_transaction(&self, tx: Transaction) -> Result<EstimateCycles> {
        let tx: packed::Transaction = tx.into();
        CyclesEstimator::new(&self.shared).run(tx)
    }
```
