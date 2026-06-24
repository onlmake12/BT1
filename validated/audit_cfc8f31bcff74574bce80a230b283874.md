Audit Report

## Title
Synchronous Blocking VM Execution in `estimate_cycles` Starves Tokio Runtime — (`rpc/src/module/chain.rs`)

## Summary

`estimate_cycles` in the Chain RPC module calls `ScriptVerifier::verify(max_block_cycles)` synchronously inside an axum/tokio async handler with no `spawn_blocking`, no cycle cap, and no concurrency limit. Concurrent requests block tokio worker threads, starving the shared async runtime and causing RPC unavailability. The `threads` config field is defined but unused in `server.rs` — the server runs entirely on the shared tokio runtime.

## Finding Description

`CyclesEstimator::run()` at `rpc/src/module/chain.rs` L2375–2405 executes the full VM synchronously:

```rust
let max_cycles = consensus.max_block_cycles;
// ...
ScriptVerifier::new(Arc::new(resolved), snapshot.as_data_loader(), consensus, Arc::new(tx_env))
    .verify(max_cycles)
```

This is called directly from `estimate_cycles` at L2119–2122 with no offloading. The RPC server (`rpc/src/server.rs`) uses axum backed by the shared tokio runtime — `handle_jsonrpc` is an `async fn` that calls `io.handle_call(call, T::default()).await` inline. A grep for `spawn_blocking`, `block_in_place`, `rayon`, or `tokio::task` in `rpc/src/**` returns zero results. The `threads` field in `RpcConfig` (`util/app-config/src/configs/rpc.rs` L42) is never consumed in `server.rs` — the server does not create a dedicated thread pool.

A `TimeoutLayer` of 30 seconds exists (`server.rs` L125–128), which caps individual request duration but does not prevent sustained saturation: an attacker continuously sends new requests to replace timed-out ones, maintaining full thread starvation indefinitely.

Exploit path:
1. Attacker has HTTP access to the RPC port (node with public RPC, or local attacker)
2. Deploys a script that loops for `max_block_cycles` (~3.5B cycles on mainnet)
3. Sends N concurrent `estimate_cycles` requests (N ≥ tokio worker thread count)
4. Each request blocks a tokio worker thread for up to 30 seconds
5. Attacker continuously replaces expiring requests
6. Tokio runtime is starved — all RPC methods, P2P networking, and block sync stall

## Impact Explanation

Blocking all tokio worker threads stalls the shared async runtime, causing at minimum full RPC unavailability (including `get_block_template` for miners) and potentially stalling P2P and block sync. This maps to **Note (0–500 points): Any local RPC API crash**, with potential escalation to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node** if the tokio runtime is confirmed shared with P2P/sync tasks (likely given the single-runtime architecture visible in `server.rs`).

## Likelihood Explanation

Requires only HTTP access to the RPC port and a live cell referencing a max-cycle script. No authentication, no PoW, no privilege. The Chain module is enabled by default. Default listen is `127.0.0.1`, limiting remote exploitation, but nodes exposing RPC publicly (wallets, dApps, public infrastructure) are directly vulnerable. The attack is repeatable and cheap to sustain.

## Recommendation

- Offload `ScriptVerifier::verify()` to `tokio::task::spawn_blocking` or a bounded rayon pool inside `CyclesEstimator::run()`
- Add a configurable `max_cycles_for_estimate` in `RpcConfig` defaulting to a fraction of `max_block_cycles`
- Add a global or per-IP concurrency semaphore for compute-heavy RPC methods (`estimate_cycles`, `dry_run_transaction`)
- Remove or wire up the unused `threads` config field

## Proof of Concept

1. Compile a CKB script that executes a tight loop consuming exactly `consensus.max_block_cycles` cycles
2. Deploy it as a live cell on a testnet node with public RPC
3. Craft an `estimate_cycles` JSON-RPC payload referencing that cell
4. Send `(tokio_worker_count + 2)` concurrent HTTP POST requests to the RPC endpoint
5. Simultaneously poll `get_block_template` — observe request timeouts for the duration of the attack
6. Confirm recovery only after all `estimate_cycles` requests expire (30s timeout) and no new ones are sent