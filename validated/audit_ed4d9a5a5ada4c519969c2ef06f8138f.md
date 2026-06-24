Audit Report

## Title
Unbounded Synchronous Script Execution Blocks Tokio Runtime via `estimate_cycles` / `dry_run_transaction` — (`rpc/src/module/chain.rs`)

## Summary
`CyclesEstimator::run` executes `ScriptVerifier::verify` synchronously on Tokio worker threads with `consensus.max_block_cycles` (3,500,000,000 cycles) as the only limit. No per-request cap, no `spawn_blocking` offload, no rate limiting, and no cancellation mechanism exists. An unprivileged caller can flood these endpoints with max-cycle scripts, saturating all Tokio worker threads and causing sustained RPC denial of service.

## Finding Description

**Root cause 1 — cycle limit is the full block maximum:**

`CyclesEstimator::run` sets `max_cycles` directly from `consensus.max_block_cycles`: [1](#0-0) 

This resolves to `TWO_IN_TWO_OUT_CYCLES × TWO_IN_TWO_OUT_COUNT = 3,500,000 × 1,000 = 3,500,000,000`: [2](#0-1) 

**Root cause 2 — synchronous blocking loop, no cancellation:**

`ScriptVerifier::verify` is a plain synchronous loop with no async yield points, no cancellation token, and no wall-clock timeout: [3](#0-2) 

**Root cause 3 — no `spawn_blocking` offload:**

A search across all `rpc/**/*.rs` files for `spawn_blocking` and `block_in_place` returns zero matches. The `#[async_trait]` impl for `ExperimentRpcImpl` calls `CyclesEstimator::run` as a plain synchronous `fn`: [4](#0-3) 

This means the blocking computation runs directly on Tokio async worker threads, not on a dedicated blocking thread pool — strictly worse than using `spawn_blocking`.

**Root cause 4 — HTTP timeout does not cancel computation:**

The `TimeoutLayer` sends `408` to the client after 30 seconds but has no mechanism to interrupt the underlying synchronous computation: [5](#0-4) 

The worker thread continues burning CPU for the full script execution duration after the HTTP response has been sent.

**Root cause 5 — no rate limiting:**

A search across all `rpc/**/*.rs` files for `rate_limit`, `RateLimit`, and `rate.limit` returns zero matches.

**Root cause 6 — no validity checks on submitted transaction:**

`CyclesEstimator`'s `CellProvider` impl treats all cells as live regardless of actual chain state, so the attacker does not need to own any live cells or pay any fee: [6](#0-5) 

`dry_run_transaction` is identically affected via the same `CyclesEstimator::run` call path: [7](#0-6) 

## Impact Explanation

Concurrent `estimate_cycles` requests with max-cycle scripts block all Tokio worker threads for the full script execution duration (potentially many seconds per request). This starves the async runtime, making the node's entire JSON-RPC interface unresponsive and degrading block processing and P2P networking. This matches two **High** impact categories: *"Vulnerabilities which could easily crash a CKB node"* and *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation

`estimate_cycles` is publicly documented, enabled by default, and reachable by any unprivileged caller. Crafting a max-cycle RISC-V script requires only basic CKB-VM knowledge. No key material, live cell ownership, fee payment, or PoW is required. The attack is fully automatable with standard HTTP tooling and is repeatable indefinitely. The `TimeoutLayer` returning `408` after 30 s does not stop the attack — the underlying threads keep running.

## Recommendation

1. **Introduce a per-request cycle cap** for `estimate_cycles` / `dry_run_transaction` significantly below `max_block_cycles` (e.g., a configurable `rpc.max_estimate_cycles`).
2. **Offload script execution to `tokio::task::spawn_blocking`** so that blocking computation does not occupy async worker threads.
3. **Implement a cancellation mechanism** (e.g., a shared atomic flag checked periodically inside the VM execution loop) so that the HTTP-level timeout or a dedicated script-execution timeout can actually abort the computation.
4. **Rate-limit** the `estimate_cycles` and `dry_run_transaction` endpoints per source IP or connection.
5. **Document** that these endpoints execute arbitrary scripts and should not be exposed on public-facing interfaces without additional protection.

## Proof of Concept

**Step 1 — Craft a max-cycle RISC-V script** that loops until `CyclesExceeded` at 3,500,000,000 cycles (a tight infinite loop in RISC-V assembly suffices).

**Step 2 — Reference the script cell** in a transaction (the cell need not be live; `CyclesEstimator` treats all cells as live).

**Step 3 — Flood `estimate_cycles`** with concurrent requests:
```bash
for i in $(seq 1 200); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{
      "id": '$i', "jsonrpc": "2.0", "method": "estimate_cycles",
      "params": [{ ... max-cycle script tx ... }]
    }' &
done
wait
```

**Expected result:** The node's Tokio runtime saturates. Subsequent RPC calls (`get_tip_block_number`, `send_transaction`, etc.) time out or queue indefinitely. CPU reaches 100% on all cores. The `TimeoutLayer` returns `408` to the attacker's clients after 30 s, but the underlying script execution threads continue running, maintaining the DoS.

### Citations

**File:** rpc/src/module/chain.rs (L2347-2361)
```rust
impl<'a> CellProvider for CyclesEstimator<'a> {
    fn cell(&self, out_point: &packed::OutPoint, eager_load: bool) -> CellStatus {
        let snapshot = self.shared.snapshot();
        snapshot
            .get_cell(out_point)
            .map(|mut cell_meta| {
                if eager_load
                    && let Some((data, data_hash)) = snapshot.get_cell_data(out_point) {
                        cell_meta.mem_cell_data = Some(data);
                        cell_meta.mem_cell_data_hash = Some(data_hash);
                    }
                CellStatus::live_cell(cell_meta)
            })  // treat as live cell, regardless of live or dead
            .unwrap_or(CellStatus::Unknown)
    }
```

**File:** rpc/src/module/chain.rs (L2380-2380)
```rust
                let max_cycles = consensus.max_block_cycles;
```

**File:** spec/src/consensus.rs (L70-84)
```rust
pub const TWO_IN_TWO_OUT_CYCLES: Cycle = 3_500_000;
/// bytes of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_BYTES: u64 = 597;
/// count of two-in-two-out txs a block should capable to package.
const TWO_IN_TWO_OUT_COUNT: u64 = 1_000;
pub(crate) const DEFAULT_EPOCH_DURATION_TARGET: u64 = 4 * 60 * 60; // 4 hours, unit: second
const MILLISECONDS_IN_A_SECOND: u64 = 1000;
const MAX_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MIN_BLOCK_INTERVAL; // 1800
const MIN_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MAX_BLOCK_INTERVAL; // 300
pub(crate) const DEFAULT_PRIMARY_EPOCH_REWARD_HALVING_INTERVAL: EpochNumber =
    4 * 365 * 24 * 60 * 60 / DEFAULT_EPOCH_DURATION_TARGET; // every 4 years

/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** script/src/verify.rs (L197-214)
```rust
    pub fn verify(&self, max_cycles: Cycle) -> Result<Cycle, Error> {
        let mut cycles: Cycle = 0;

        // Now run each script group
        for (_hash, group) in self.groups() {
            // max_cycles must reduce by each group exec
            let used_cycles = self
                .verify_script_group(group, max_cycles - cycles)
                .map_err(|e| {
                    #[cfg(feature = "logging")]
                    logging::on_script_error(_hash, &self.hash(), &e);
                    e.source(group)
                })?;

            cycles = wrapping_cycles_add(cycles, used_cycles, group)?;
        }
        Ok(cycles)
    }
```

**File:** rpc/src/module/experiment.rs (L228-233)
```rust
#[async_trait]
impl ExperimentRpc for ExperimentRpcImpl {
    fn dry_run_transaction(&self, tx: Transaction) -> Result<EstimateCycles> {
        let tx: packed::Transaction = tx.into();
        CyclesEstimator::new(&self.shared).run(tx)
    }
```

**File:** rpc/src/server.rs (L125-128)
```rust
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
```
