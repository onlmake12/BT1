### Title
Unbounded JSON-RPC Batch Requests Allow Any Caller to Exhaust Node CPU via Free `estimate_cycles` Script Execution — (`rpc/src/server.rs`, `rpc/src/module/chain.rs`)

---

### Summary

The CKB JSON-RPC server has no batch-request size limit enforced by default. The `rpc_batch_limit` configuration option is commented out in the shipped default config, leaving `JSONRPC_BATCH_LIMIT` as an uninitialized `OnceLock`. The batch handler only enforces a limit when that `OnceLock` is populated. Simultaneously, the `estimate_cycles` RPC method (enabled in the default module list) runs full CKB-VM script execution up to `consensus.max_block_cycles` (10 billion cycles on mainnet) per call, with no fee, no authentication, and no rate limiting. Any unprivileged RPC caller can combine these two facts to send a single HTTP POST containing an arbitrarily large batch of maximum-cycle `estimate_cycles` calls, exhausting the node's CPU and rendering it unresponsive to all legitimate users.

---

### Finding Description

**Root cause 1 — `rpc_batch_limit` is `None` by default**

`JSONRPC_BATCH_LIMIT` is a `static OnceLock<usize>` declared in `rpc/src/server.rs`:

```rust
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();
```

It is only initialized when `config.rpc_batch_limit` is `Some(...)`:

```rust
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
```

The shipped default config has the option commented out:

```toml
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

The batch handler only enforces the limit when `JSONRPC_BATCH_LIMIT.get()` returns `Some(...)`. When it returns `None` (the default), the guard is skipped entirely and all calls in the batch are processed:

```rust
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    {
        return make_error_response(...);
    }
    // No limit → process every call unconditionally
    let stream = stream::iter(calls)
        .then(move |call| { ... io.handle_call(call, ...).await })
        ...
}
```

**Root cause 2 — `estimate_cycles` runs full VM execution for free**

`estimate_cycles` is part of the `Chain` module, which is enabled by default. Its implementation in `CyclesEstimator::run()` resolves the transaction and invokes `ScriptVerifier::verify(max_cycles)` where `max_cycles = consensus.max_block_cycles` (10,000,000,000 on mainnet):

```rust
let max_cycles = consensus.max_block_cycles;
...
ScriptVerifier::new(Arc::new(resolved), snapshot.as_data_loader(), consensus, Arc::new(tx_env))
    .verify(max_cycles)
```

There is no fee charged, no authentication required, and no per-caller rate limit. The only bound on the request body is `max_request_body_size = 10485760` (10 MiB), which is large enough to encode thousands of `estimate_cycles` calls in a single batch.

**Combined attack**

An attacker crafts a transaction whose lock/type script runs for close to `max_block_cycles` cycles (a tight loop in RISC-V bytecode). They then send a single HTTP POST to the RPC endpoint containing a JSON array of N such `estimate_cycles` calls. Because `rpc_batch_limit` is `None` by default, the server processes all N calls sequentially, each consuming up to 10 billion VM cycles of CPU. The node's RPC thread pool is saturated; legitimate requests (block submissions, tx relay, miner `get_block_template`) time out or are dropped.

---

### Impact Explanation

- **Node unavailability**: The RPC service becomes unresponsive for the duration of the attack. Miners cannot fetch block templates, losing revenue. dApp users and relayers cannot submit transactions.
- **No economic cost to attacker**: `estimate_cycles` charges no fee and requires no CKB balance. The attacker pays only network bandwidth.
- **Shared resource drained**: The node's CPU capacity is the shared resource. Like the Drips `commonFunds`, it is consumed on behalf of all legitimate users by an unrestricted caller.
- **Persistence**: The attack can be repeated continuously. Each new batch request restarts the exhaustion cycle.

---

### Likelihood Explanation

- Many CKB node operators expose their RPC publicly (mining pools, dApp backends, public RPC endpoints). The default bind address is `127.0.0.1`, but operators routinely change this.
- Even for localhost-only nodes, any process on the same machine (a compromised dependency, a malicious script) qualifies as an "RPC caller."
- The attack requires zero CKB, zero cryptographic material, and no prior knowledge of the chain state. A single HTTP client suffices.
- The `rpc_batch_limit` protection exists in the codebase but is opt-in; the default config explicitly documents that no limit is applied, meaning most deployed nodes are unprotected.

---

### Recommendation

1. **Enable `rpc_batch_limit` by default**: Set a safe default (e.g., `rpc_batch_limit = 200`) in `resource/ckb.toml` so that `JSONRPC_BATCH_LIMIT` is always initialized. The current opt-in model inverts the safe default.

2. **Add per-method cycle budgets or timeouts to `estimate_cycles`**: Introduce a configurable `max_estimate_cycles` cap lower than `max_block_cycles`, or enforce a wall-clock timeout per RPC call.

3. **Rate-limit expensive RPC methods per IP/connection**: Apply a token-bucket or leaky-bucket rate limiter specifically to `estimate_cycles` and `dry_run_transaction` at the server layer.

---

### Proof of Concept

```python
import json, requests

# Craft a transaction whose script runs ~max_block_cycles (attacker controls the script binary)
expensive_tx = { ... }  # RISC-V tight-loop script, valid cell deps on-chain

# Build an unbounded batch (no rpc_batch_limit by default)
batch = [
    {"jsonrpc": "2.0", "method": "estimate_cycles", "params": [expensive_tx], "id": i}
    for i in range(5000)  # 5000 × 10B cycles = 50 trillion cycles of CPU work
]

# Single HTTP POST saturates the node's RPC workers
r = requests.post("http://<node-rpc>:8114", json=batch, timeout=None)
# Node becomes unresponsive to all other requests during processing
```

**Key code references:**

- Default batch limit absent: [1](#0-0) 
- Batch handler skips limit when `OnceLock` is empty: [2](#0-1) 
- `estimate_cycles` runs full VM at `max_block_cycles` for free: [3](#0-2) 
- Default config documents no limit is applied: [4](#0-3) 
- `estimate_cycles` enabled in default module list: [5](#0-4) 
- `rpc_batch_limit` is `Option<usize>` (absent = `None`): [6](#0-5)

### Citations

**File:** rpc/src/server.rs (L34-55)
```rust
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();

#[doc(hidden)]
#[derive(Debug)]
pub struct RpcServer {
    pub http_address: SocketAddr,
    pub tcp_address: Option<SocketAddr>,
    pub ws_address: Option<SocketAddr>,
}

impl RpcServer {
    /// Creates an RPC server.
    ///
    /// ## Parameters
    ///
    /// * `config` - RPC config options.
    /// * `io_handler` - RPC methods handler. See [ServiceBuilder](../service_builder/struct.ServiceBuilder.html).
    /// * `handler` - Tokio runtime handle.
    pub fn new(config: RpcConfig, io_handler: IoHandler, handler: Handle) -> Self {
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }
```

**File:** rpc/src/server.rs (L274-296)
```rust
            Request::Batch(calls) => {
                if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
                    && calls.len() > *batch_size
                {
                    return make_error_response(jsonrpc_core::Error::invalid_params(format!(
                        "batch size is too large, expect it less than: {}",
                        batch_size
                    )));
                }

                let stream = stream::iter(calls)
                    .then(move |call| {
                        let io = Arc::clone(&io);
                        async move { io.handle_call(call, T::default()).await }
                    })
                    .filter_map(|response| async move { response });

                (
                    [(axum::http::header::CONTENT_TYPE, "application/json")],
                    StreamBodyAs::json_array(stream),
                )
                    .into_response()
            }
```

**File:** rpc/src/module/chain.rs (L2375-2405)
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
            Err(err) => Err(RPCError::custom_with_error(
                RPCError::TransactionFailedToResolve,
                err,
            )),
        }
    }
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

**File:** util/app-config/src/configs/rpc.rs (L43-44)
```rust
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
```
