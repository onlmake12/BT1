Audit Report

## Title
Unbounded Synchronous VM Execution in `estimate_cycles` Blocks Tokio Executor Threads — (File: `rpc/src/module/chain.rs`)

## Summary

The `estimate_cycles` RPC handler invokes `ScriptVerifier::verify(max_block_cycles)` synchronously on a Tokio worker thread with no `spawn_blocking` offload, no per-IP rate limiting, and no concurrency cap. An attacker who can reach the RPC port can saturate all Tokio worker threads with concurrent max-cycle transactions, rendering the node's RPC, P2P relay, and block processing completely unresponsive.

## Finding Description

**Handler dispatch (server.rs L258):** `io.handle_call(call, T::default()).await` dispatches the synchronous `estimate_cycles` fn directly on the calling async task. `jsonrpc_core`'s `MetaIoHandler` does not use `spawn_blocking` for synchronous handlers — it polls the sync function inline, blocking the Tokio worker thread for the full duration.

**Confirmed: no `spawn_blocking` anywhere in `rpc/src/`** — a grep across all files in `rpc/src/**/*.rs` returns zero matches for `spawn_blocking`.

**VM execution (chain.rs L2375–2405):** `CyclesEstimator::run` calls `ScriptVerifier::new(...).verify(max_cycles)` where `max_cycles = consensus.max_block_cycles` (3.5 billion on mainnet). This is a full RISC-V VM execution, synchronous, with no lower cap.

**Handler entry (chain.rs L2119–2122):**
```rust
fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles> {
    let tx: packed::Transaction = tx.into();
    CyclesEstimator::new(&self.shared).run(tx)
}
```

**No rate limiting (server.rs L119–129):** The axum router has only `CorsLayer::permissive()` and a 30-second `TimeoutLayer`. No per-IP limiter, no semaphore, no concurrent-request cap exists anywhere in the RPC stack.

**Timeout ineffective:** The `TimeoutLayer` fires at the HTTP future level, but because the VM runs synchronously on the executor thread, the thread remains blocked until the VM completes — the timeout cannot preempt a blocking CPU computation.

**Tokio runtime (util/runtime/src/native.rs L86–88):** Uses `Builder::new_multi_thread()` with `worker_threads = available_parallelism()` (CPU core count). With N concurrent attacker requests (N ≥ core count), all worker threads are blocked, starving every other async task.

## Impact Explanation

Matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.** With all Tokio worker threads blocked, the node cannot process P2P messages, relay blocks, or respond to any other RPC call — effectively a full node crash for the duration of the attack. The attack is repeatable with no state required.

## Likelihood Explanation

The `estimate_cycles` method is in the `Chain` module, which is enabled by default. The attack requires only HTTP POST access to the RPC port. While the default binding is `127.0.0.1:8114`, nodes serving dApp backends routinely expose this port to application servers, making it reachable by any client of those backends. Crafting a max-cycle script (an infinite loop capped by the cycle limit) is trivial. No keys, funds, or privileged role are required.

## Recommendation

1. Wrap the VM execution in `tokio::task::spawn_blocking` so it runs on the blocking thread pool, freeing Tokio worker threads and allowing the `TimeoutLayer` to actually cancel the future.
2. Add a `tokio::sync::Semaphore` to cap concurrent `estimate_cycles` executions (e.g., to `num_cpus / 2`).
3. Add per-IP or global rate limiting via a `tower` middleware layer (e.g., `tower_governor`) before the axum router dispatches to the handler.
4. Consider a lower per-call cycle cap for `estimate_cycles` (e.g., a configurable fraction of `max_block_cycles`).

## Proof of Concept

1. Deploy a CKB node with the `Chain` RPC module enabled (default).
2. Craft a transaction whose lock script loops for `max_block_cycles` cycles (e.g., using an always-loop script deployed on testnet).
3. Fire `N` concurrent HTTP POST requests to `estimate_cycles` where `N` equals the node's CPU core count:
```python
import asyncio, aiohttp

MAX_CYCLE_TX = { ... }  # transaction referencing a max-cycle loop script

async def flood(session, url):
    payload = {"id": 1, "jsonrpc": "2.0", "method": "estimate_cycles", "params": [MAX_CYCLE_TX]}
    while True:
        try:
            await session.post(url, json=payload)
        except Exception:
            pass

async def main():
    url = "http://127.0.0.1:8114"
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*[flood(session, url) for _ in range(32)])

asyncio.run(main())
```
4. Observe that concurrent `get_tip_block_number` calls time out and P2P block relay stalls, confirming full executor thread exhaustion.