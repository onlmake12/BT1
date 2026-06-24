Audit Report

## Title
Unauthenticated `estimate_cycles` Executes Unbounded Scripts on Tokio Worker Threads, Enabling Node DoS — (File: `rpc/src/module/chain.rs`)

## Summary
`CyclesEstimator::run()` calls `ScriptVerifier::verify(max_block_cycles)` synchronously on a Tokio worker thread with no `spawn_blocking` wrapper, no per-call cycle cap, and no rate limiting. Any caller with RPC access can flood the endpoint with max-cycle transactions, blocking all Tokio worker threads and rendering the node unresponsive. The `CellProvider` intentionally treats spent cells as live, removing the requirement to own any live cell with an expensive script.

## Finding Description
`estimate_cycles` is registered in the default-enabled `Chain` RPC module and delegates directly to `CyclesEstimator::run()`:

```rust
// rpc/src/module/chain.rs:2119-2122
fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles> {
    let tx: packed::Transaction = tx.into();
    CyclesEstimator::new(&self.shared).run(tx)
}
```

Inside `run()`, the cycle limit is set to the full block budget and `ScriptVerifier::verify` is called synchronously — no `spawn_blocking`, no timeout on the VM itself:

```rust
// rpc/src/module/chain.rs:2380, 2389
let max_cycles = consensus.max_block_cycles;
...
.verify(max_cycles)
```

A grep of `rpc/src/module/chain.rs` confirms zero uses of `spawn_blocking` or `block_in_place`. This means the CPU-bound VM execution runs directly on a Tokio worker thread, starving the async executor.

The `CellProvider` implementation explicitly treats every cell — live or dead — as live:

```rust
// rpc/src/module/chain.rs:2358-2359
CellStatus::live_cell(cell_meta)
})  // treat as live cell, regardless of live or dead
```

The HTTP server applies a 30-second `TimeoutLayer`, but this only sends a 408 response to the client; it cannot preempt a blocking computation already occupying a worker thread. The thread remains blocked until the VM exhausts `max_block_cycles`.

The RPC `Config` struct has no per-method rate limiting, concurrency cap, or authentication fields. The default configuration enables the `Chain` module and binds to `127.0.0.1:8114`, but nodes that expose RPC publicly (common for infrastructure and dApp backends) are fully exposed.

## Impact Explanation
Matches **High — Vulnerabilities which could easily crash a CKB node**. An attacker with RPC access sends N concurrent `estimate_cycles` requests carrying a max-cycle script. Each request occupies one Tokio worker thread for up to the full VM execution duration (potentially tens of seconds at `max_block_cycles = 3,500,000,000`). Once all worker threads are saturated, the node's async runtime stalls: block template generation, transaction relay, chain sync responses, and all other RPC calls become unresponsive. The node effectively crashes from the perspective of the network.

## Likelihood Explanation
- The `Chain` module is included in the default `modules` list in `resource/ckb.toml`.
- No fee, signature, or on-chain asset is required.
- The "dead cells as live" design means the attacker does not need to own any live cell — any historical system script cell suffices as the script carrier.
- The attack is repeatable indefinitely at negligible cost (network bandwidth only).
- The only barrier is that the default listen address is `127.0.0.1`; nodes that bind to a public interface (a common production configuration) are directly reachable.

## Recommendation
1. **Wrap `ScriptVerifier::verify` in `spawn_blocking`** so it does not occupy Tokio worker threads; pair with a per-call timeout that actually cancels the blocking task.
2. **Cap the cycle limit for `estimate_cycles`** at `max_tx_verify_cycles` (configured at `70,000,000` in `ckb.toml`) rather than `max_block_cycles`.
3. **Add a concurrency semaphore** at the RPC handler level for compute-heavy endpoints (`estimate_cycles`, `dry_run_transaction`, `test_tx_pool_accept`).
4. **Reject dead cells** in `CyclesEstimator`'s `CellProvider` to prevent referencing scripts the caller does not control.

## Proof of Concept
1. Deploy a RISC-V ELF that loops for `max_block_cycles` cycles on-chain, then spend the cell (making it dead).
2. Craft a transaction whose input references the now-dead cell (the `CellProvider` will treat it as live).
3. From a machine with RPC access, run:

```bash
for i in $(seq 1 <num_rpc_threads>); do
  curl -s -X POST http://<node>:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"estimate_cycles","params":[<crafted_tx_json>],"id":1}' &
done
wait
```

Each concurrent call blocks one Tokio worker thread. Once all threads are occupied, issue any other RPC call (e.g., `get_tip_block_number`) and observe it hanging until the 30-second HTTP timeout fires — confirming the async runtime is fully saturated. The node's block relay and sync will also stall during this window.