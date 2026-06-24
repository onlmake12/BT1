Audit Report

## Title
Unbounded CPU Exhaustion via Repeated `estimate_cycles` RPC Calls Without Rate Limiting - (File: `rpc/src/module/chain.rs`)

## Summary
The `estimate_cycles` RPC handler executes a full CKB-VM script verification synchronously on the Tokio executor thread, with no rate limiting, concurrency cap, or `spawn_blocking` offload. An attacker with access to the RPC port can flood this endpoint with max-cycle transactions, blocking all Tokio worker threads and rendering the node unresponsive.

## Finding Description
The handler at `rpc/src/module/chain.rs` lines 2119–2122 is a synchronous `fn` that directly calls `CyclesEstimator::new(&self.shared).run(tx)`. Inside `run` (lines 2375–2405), `ScriptVerifier::new(...).verify(max_cycles)` is called synchronously with `max_cycles = consensus.max_block_cycles` (3.5 billion cycles on mainnet). This is a full RISC-V VM execution with no early exit.

The handler is declared as a synchronous `fn` (line 1530), not `async fn`. When `handle_jsonrpc` dispatches it via `io.handle_call(call, T::default()).await` (server.rs line 258), jsonrpc-core executes the synchronous handler inline on the current Tokio worker thread, blocking it for the full duration of the VM run.

The axum router (server.rs lines 119–129) applies only a `CorsLayer::permissive()` and a `TimeoutLayer` of 30 seconds — no per-IP rate limiting, no global concurrency cap, no semaphore. A grep across all of `rpc/src/` confirms zero uses of `spawn_blocking`, `block_in_place`, `Semaphore`, or any rate-limiting primitive. The `TimeoutLayer` cannot preempt a synchronously blocked thread; it fires at the HTTP future layer, but the thread itself remains occupied until the VM completes.

With N concurrent attacker connections (N ≥ Tokio worker thread count), all worker threads are blocked simultaneously, starving block relay, P2P message handling, and all other RPC calls.

## Impact Explanation
This matches **High: Vulnerabilities which could easily crash a CKB node**. The node becomes fully unresponsive — no RPC responses, no P2P block relay, no block processing — for as long as the attacker sustains the flood. The attack is repeatable with no state required and no on-chain cost.

## Likelihood Explanation
The `estimate_cycles` endpoint is part of the Chain module, which is enabled by default. The attack requires only HTTP POST access to the RPC port. While the default bind address is `127.0.0.1:8114`, nodes serving application backends (exchanges, dApps, wallets) routinely expose this port to internal networks. Crafting a max-cycle script (a simple loop capped by the cycle limit) is trivial. No keys, funds, or privileged role are required.

## Recommendation
1. Offload VM execution with `tokio::task::spawn_blocking` so the Tokio executor thread is not blocked and the `TimeoutLayer` can actually cancel the future.
2. Add a `tokio::sync::Semaphore` to cap the number of concurrent `estimate_cycles` executions (e.g., to `num_cpus / 2`).
3. Add per-IP or global rate limiting middleware (e.g., `tower_governor`) before the axum router dispatches to the handler.
4. Consider a lower per-call cycle cap for `estimate_cycles` (e.g., a configurable fraction of `max_block_cycles`) to bound worst-case execution time.

## Proof of Concept
1. Deploy a CKB node with default configuration (Chain module enabled, RPC on `127.0.0.1:8114`).
2. Craft a transaction whose lock script loops for `max_block_cycles` cycles (e.g., using the always-loop test script).
3. Launch 32 concurrent HTTP POST requests to `estimate_cycles` with this transaction (matching or exceeding the Tokio worker thread count):
```python
import asyncio, aiohttp, json

MAX_CYCLE_TX = { ... }  # transaction with max-cycle loop script

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
4. From a separate client, call `get_tip_block_number` — it will time out, confirming full executor thread exhaustion.