Audit Report

## Title
Synchronous Blocking VM Execution in `estimate_cycles` Saturates RPC Tokio Runtime — (`rpc/src/module/chain.rs`)

## Summary

`estimate_cycles` calls `ScriptVerifier::verify(max_block_cycles)` synchronously on a tokio worker thread with no offloading, no cycle cap below `consensus.max_block_cycles`, and no effective timeout. The RPC server has migrated from `jsonrpc-http-server` to axum-on-tokio, meaning blocking VM execution directly stalls tokio worker threads in the RPC runtime. Concurrent requests saturate the RPC runtime, rendering all RPC methods unresponsive.

## Finding Description

`CyclesEstimator::run()` sets `max_cycles = consensus.max_block_cycles` and calls `ScriptVerifier::new(...).verify(max_cycles)` — a fully synchronous, blocking VM execution — with no upper bound below the consensus maximum:

```rust
// rpc/src/module/chain.rs L2380, L2383-2389
let max_cycles = consensus.max_block_cycles;
...
ScriptVerifier::new(
    Arc::new(resolved),
    snapshot.as_data_loader(),
    consensus,
    Arc::new(tx_env),
)
.verify(max_cycles)
```

`estimate_cycles` dispatches directly to this with no interposition:

```rust
// rpc/src/module/chain.rs L2119-2122
fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles> {
    let tx: packed::Transaction = tx.into();
    CyclesEstimator::new(&self.shared).run(tx)
}
```

The RPC server is **axum-on-tokio**, not a dedicated thread pool. `handle_jsonrpc` is an `async fn` that awaits `io.handle_call(call, T::default()).await` on a tokio worker thread. When the synchronous handler runs, it blocks that tokio worker thread for the full duration of VM execution. A `TimeoutLayer::with_status_code(..., Duration::from_secs(30))` is applied, but async middleware cannot preempt a blocking synchronous call that never yields to the runtime — the timeout future cannot be polled while the thread is blocked.

A grep for `spawn_blocking`, `block_in_place`, `rayon`, or `tokio::task` in `rpc/src/**` returns **no results** — the VM execution is never offloaded.

The `Launcher` struct holds two separate handles (`async_handle` and `rpc_handle`), confirming the RPC server runs on its own dedicated tokio runtime, separate from the main node runtime. Saturating the RPC runtime does not directly stall p2p/sync, but it renders **all RPC methods** — including `get_block_template` — completely unresponsive for the duration of the attack.

The `threads` config field in `RpcConfig` is unused by the current axum server; it is a leftover from the old `jsonrpc-http-server` implementation and provides no protection.

## Impact Explanation

Sending N concurrent `estimate_cycles` requests (N ≥ tokio worker thread count for the RPC runtime) with a max-cycle looping script freezes the entire RPC API. This matches the allowed CKB bounty impact: **"Any local RPC API crash" (Note, 0–500 points)**. The node itself continues to operate (separate runtime), so "crash a CKB node" (High) is not met. If the targeted node serves miners via public RPC, `get_block_template` unavailability could indirectly affect mining, but this requires public RPC exposure and is not the primary impact.

## Likelihood Explanation

Requires only HTTP access to the RPC port and a live cell referencing a max-cycle looping script. No authentication, no PoW, no privilege. The Chain module is enabled by default. The practical barrier is the default `127.0.0.1` listen address; nodes with publicly exposed RPC (wallet/dApp infrastructure) are directly vulnerable. The attack is repeatable and requires no special knowledge beyond the RPC API.

## Recommendation

- Offload `ScriptVerifier::verify()` to `tokio::task::spawn_blocking` within `CyclesEstimator::run()` so tokio worker threads are not blocked
- Add a configurable `max_cycles_for_estimate` cap in `RpcConfig`, defaulting to a fraction of `max_block_cycles`
- Add a global or per-IP concurrency semaphore for `estimate_cycles` to bound simultaneous VM executions

## Proof of Concept

1. Deploy a CKB script that executes a tight loop consuming exactly `max_block_cycles` cycles and publish it as a live cell
2. Determine the tokio worker thread count for the RPC runtime (defaults to number of CPU cores)
3. Send `thread_count + 1` concurrent `estimate_cycles` JSON-RPC POST requests referencing that cell to the RPC endpoint
4. While those requests are in-flight, issue a `get_block_template` call
5. Observe that `get_block_template` does not respond until at least one VM execution completes, confirming full RPC runtime saturation