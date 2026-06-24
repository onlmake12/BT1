Audit Report

## Title
Synchronous Blocking Script Execution in `estimate_cycles` RPC Enables Local RPC Thread Exhaustion — (File: `rpc/src/module/chain.rs`)

## Summary

`CyclesEstimator::run` in `rpc/src/module/chain.rs` executes `ScriptVerifier::verify(max_cycles)` synchronously on the tokio async worker thread, using `consensus.max_block_cycles` as the cycle ceiling — a limit far exceeding the tx-pool's per-transaction cap of 70 M cycles. With no `spawn_blocking` offload, no per-method rate limit, and no default batch size cap, a local RPC caller can saturate the tokio worker thread pool by sending concurrent or batched `estimate_cycles` requests carrying maximum-cycle scripts, degrading or crashing RPC service.

## Finding Description

`CyclesEstimator::run` at `rpc/src/module/chain.rs:2380` sets `max_cycles = consensus.max_block_cycles` and calls `.verify(max_cycles)` directly on the calling thread with no `spawn_blocking` or equivalent offload. A grep across all of `rpc/src/` confirms zero uses of `spawn_blocking`, `block_in_place`, or any thread-pool delegation. The call therefore blocks the tokio async worker thread for the full duration of VM execution.

Batch requests are handled at `rpc/src/server.rs:274-295` via `stream::iter(calls).then(...)`, which processes each call sequentially on the same async task. The batch size guard at line 275 only fires when `JSONRPC_BATCH_LIMIT` is set, which requires `rpc_batch_limit` to be configured — commented out by default in `resource/ckb.toml:208`. An attacker can therefore send a single HTTP request containing an arbitrarily large batch of `estimate_cycles` calls.

A `TimeoutLayer` of 30 seconds exists at `rpc/src/server.rs:125-128`, which cancels the HTTP response future after 30 s. However, because `ScriptVerifier::verify` is a synchronous blocking call running directly on the tokio worker thread, dropping the outer future does not preempt the in-progress blocking computation — the thread remains occupied until the synchronous call returns or the 30-second wall-clock limit is reached. An attacker sending N concurrent requests can hold N tokio worker threads blocked for up to 30 seconds each, and can repeat the attack continuously.

The tx-pool path enforces `max_tx_verify_cycles = 70_000_000` (resource/ckb.toml:215) and requires `min_fee_rate` payment; `estimate_cycles` bypasses both guards entirely.

## Impact Explanation

Saturating the tokio worker thread pool stalls all async work dispatched to that runtime, including other RPC handlers. This constitutes a local RPC API crash/denial-of-service. The default listen address is `127.0.0.1:8114` (loopback), so the attacker must be a local process — matching the "supported local CLI/RPC user" attacker role. This maps to: **Note (0–500 points) — Any local RPC API crash.**

If the RPC shares its tokio runtime with P2P networking or block-processing tasks (not confirmed from available code), the impact could escalate to node-level degradation, but that cannot be asserted from the evidence reviewed.

## Likelihood Explanation

The `Chain` module is enabled by default (`resource/ckb.toml:190`). Any process on the same host — a miner, indexer, or dApp — can reach `127.0.0.1:8114` without credentials. No on-chain funds, mining power, or peer connection are required. The attack requires only knowledge of the public RPC documentation and the ability to craft a looping RISC-V script. Operators who expose the RPC on a non-loopback address widen the attacker surface to the network.

## Recommendation

1. **Cap `estimate_cycles` at `max_tx_verify_cycles`** instead of `consensus.max_block_cycles` to eliminate the cycle-budget asymmetry with the tx-pool path.
2. **Offload script execution via `tokio::task::spawn_blocking`** so that blocking VM work does not occupy tokio async worker threads.
3. **Enable `rpc_batch_limit` by default** (e.g., 100) in `resource/ckb.toml` to prevent batch amplification.
4. **Add per-IP or per-connection rate limiting** for computationally expensive RPC methods.

## Proof of Concept

```python
import json, requests, threading

# Any transaction whose lock script loops for max_block_cycles cycles.
looping_tx = { "cell_deps": [], "inputs": [...], "outputs": [...],
               "outputs_data": [], "version": "0x0", "witnesses": [...] }

# Single HTTP request with unbounded batch (no rpc_batch_limit by default).
batch = [{"jsonrpc":"2.0","id":i,"method":"estimate_cycles","params":[looping_tx]}
         for i in range(200)]

def flood():
    try:
        requests.post("http://127.0.0.1:8114", json=batch, timeout=35)
    except Exception:
        pass

# Launch concurrent requests to saturate tokio worker threads.
threads = [threading.Thread(target=flood) for _ in range(16)]
for t in threads: t.start()
for t in threads: t.join()

# During the above, get_tip_block_number and other RPC calls will time out
# because all tokio worker threads are blocked on ScriptVerifier::verify().
```