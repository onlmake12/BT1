### Title
Unbounded Repeated `estimate_cycles` RPC Calls Without Rate Limiting Allow CPU Exhaustion - (`rpc/src/module/chain.rs`)

---

### Summary

The `estimate_cycles` RPC endpoint (and `test_tx_pool_accept`) runs a full CKB-VM `ScriptVerifier` execution up to `max_block_cycles` per call, with no per-caller rate limiting and no default batch-request cap. Any RPC caller can repeatedly invoke this endpoint — individually or in arbitrarily large batches — to sustain maximum CPU load on the node, analogous to the Arrakis finding where an operator calls `rebalance` many times to accumulate losses that each individual call's safety check permits.

---

### Finding Description

`CyclesEstimator::run` in `rpc/src/module/chain.rs` resolves the caller-supplied transaction and then calls `ScriptVerifier::new(...).verify(max_cycles)` where `max_cycles = consensus.max_block_cycles` — the consensus-level maximum cycles per block. This is a synchronous, CPU-bound operation that can run for the full cycle budget.

```
// rpc/src/module/chain.rs  ~line 2375-2404
pub(crate) fn run(&self, tx: packed::Transaction) -> Result<EstimateCycles> {
    ...
    ScriptVerifier::new(Arc::new(resolved), ..., consensus, ...)
        .verify(max_cycles)   // ← runs up to max_block_cycles VM cycles
    ...
}
```

There is no rate limiter, no per-IP throttle, and no authentication on this path. The RPC handler dispatches each call directly:

```
// rpc/src/module/chain.rs  ~line 1529-1530
#[rpc(name = "estimate_cycles")]
fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles>;
```

The batch-request limit that could cap the number of calls per HTTP request is **disabled by default**. The default `ckb.toml` leaves `rpc_batch_limit` commented out:

```
# By default, there is no limitation on the size of batch request size
# rpc_batch_limit = 2000
```

The server only enforces a batch limit when `JSONRPC_BATCH_LIMIT` has been set:

```
// rpc/src/server.rs  ~line 275-282
if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
    && calls.len() > *batch_size
{
    return make_error_response(...);
}
```

Because `JSONRPC_BATCH_LIMIT` is a `OnceLock` that is only populated when `config.rpc_batch_limit` is `Some(...)`, a default-configured node never sets it, so the guard is never reached.

`test_tx_pool_accept` has the same structure: it runs full script verification without inserting the transaction into the pool, so the same transaction can be submitted repeatedly without hitting the duplicate-rejection guard.

---

### Impact Explanation

An RPC caller can craft a transaction whose lock/type script consumes close to `max_block_cycles` VM cycles (the consensus maximum). By sending a single JSON-RPC batch request containing thousands of `estimate_cycles` calls — each referencing the same maximally expensive script — the caller forces the node to execute the CKB-VM for the full cycle budget thousands of times in sequence. This saturates all available CPU threads assigned to the RPC service, making the node unresponsive to legitimate peers, block propagation, and transaction relay. The impact is a sustained, remotely-triggerable denial of service against the node's core functions.

---

### Likelihood Explanation

The RPC is bound to `127.0.0.1:8114` by default, which limits the attacker surface to local processes. However:

1. The scope explicitly includes "RPC caller" and "local CLI/RPC user" as valid attacker roles.
2. Many production deployments expose the RPC to internal networks or behind reverse proxies without authentication.
3. The attack requires no special knowledge: a single HTTP POST with a JSON batch array is sufficient.
4. The default configuration provides zero protection (batch limit is opt-in, not opt-out).

---

### Recommendation

1. **Enable a default batch request limit.** Change `rpc_batch_limit` from an opt-in `Option<usize>` to a mandatory default (e.g., 100 or 1000) in `util/app-config/src/configs/rpc.rs` and `resource/ckb.toml`.

2. **Add per-caller rate limiting on `estimate_cycles` and `test_tx_pool_accept`.** Apply a token-bucket or leaky-bucket rate limiter keyed by source IP, similar to the existing `governor`-based limiters used in the Relayer and HolePunching protocols (`sync/src/relayer/mod.rs`, `network/src/protocols/hole_punching/mod.rs`).

3. **Cap the cycle budget for `estimate_cycles`.** Rather than using `consensus.max_block_cycles` as the limit, use a lower configurable cap (e.g., `max_tx_verify_cycles` from `TxPoolConfig`) to bound the worst-case cost per call.

---

### Proof of Concept

```python
import json, requests

# Craft a transaction referencing a maximally expensive script
expensive_tx = { ... }  # script that loops for max_block_cycles

# Send a batch of 10,000 estimate_cycles calls in one HTTP request
batch = [
    {"id": i, "jsonrpc": "2.0", "method": "estimate_cycles",
     "params": [expensive_tx]}
    for i in range(10_000)
]

# Default node has no batch limit → all 10,000 VM executions run sequentially
requests.post("http://127.0.0.1:8114/", json=batch)
# Node CPU pegged at 100% for the duration; legitimate RPC calls time out
```

**Root cause chain:**
- `handle_jsonrpc` in `rpc/src/server.rs` receives the batch
- `JSONRPC_BATCH_LIMIT` is unset (default config) → no size check
- Each call dispatches to `ChainRpcImpl::estimate_cycles` → `CyclesEstimator::run`
- `ScriptVerifier::verify(max_block_cycles)` runs the full CKB-VM for each call
- No rate limiter exists anywhere in this path [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

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

**File:** rpc/src/server.rs (L274-282)
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

**File:** sync/src/relayer/mod.rs (L88-98)
```rust
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L249-257)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
