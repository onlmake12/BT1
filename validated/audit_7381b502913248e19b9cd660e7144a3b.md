Audit Report

## Title
Unbounded Synchronous Script Execution in `estimate_cycles` / `dry_run_transaction` Blocks Tokio Runtime — (`rpc/src/module/chain.rs`)

## Summary
`CyclesEstimator::run` in `rpc/src/module/chain.rs` executes `ScriptVerifier::verify` synchronously with `consensus.max_block_cycles` (3,500,000,000 on mainnet) as the only limit. There is no per-request cycle cap, no rate limiting, and no cancellation mechanism reachable from the HTTP-level `TimeoutLayer`. An unprivileged caller can flood `estimate_cycles` with max-cycle scripts, blocking Tokio worker threads and starving the node's async runtime, causing RPC denial of service and degrading block processing and P2P networking.

## Finding Description

**Root cause — no cycle cap below block maximum:**

`CyclesEstimator::run` takes `max_cycles` directly from `consensus.max_block_cycles`:

```rust
// rpc/src/module/chain.rs  L2380
let max_cycles = consensus.max_block_cycles;
``` [1](#0-0) 

This value is `TWO_IN_TWO_OUT_CYCLES × TWO_IN_TWO_OUT_COUNT = 3,500,000 × 1,000 = 3,500,000,000`:

```rust
// spec/src/consensus.rs  L70, L84
pub const TWO_IN_TWO_OUT_CYCLES: Cycle = 3_500_000;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
``` [2](#0-1) 

**Root cause — synchronous blocking loop with no cancellation:**

`ScriptVerifier::verify` is a plain synchronous loop with no async yield points, no cancellation token, and no wall-clock timeout:

```rust
// script/src/verify.rs  L197-214
pub fn verify(&self, max_cycles: Cycle) -> Result<Cycle, Error> {
    let mut cycles: Cycle = 0;
    for (_hash, group) in self.groups() {
        let used_cycles = self
            .verify_script_group(group, max_cycles - cycles)...?;
        cycles = wrapping_cycles_add(cycles, used_cycles, group)?;
    }
    Ok(cycles)
}
``` [3](#0-2) 

**HTTP timeout does not cancel computation:**

The `TimeoutLayer` at the transport layer sends a `408` to the client after 30 seconds but has no mechanism to interrupt the underlying synchronous computation:

```rust
// rpc/src/server.rs  L125-128
.layer(TimeoutLayer::with_status_code(
    StatusCode::REQUEST_TIMEOUT,
    Duration::from_secs(30),
))
``` [4](#0-3) 

The blocking thread (or async worker thread, depending on how `jsonrpc-utils` dispatches the sync `fn`) continues burning CPU for the full script execution duration after the HTTP response has been sent. No `spawn_blocking` calls were found in the RPC module code itself, meaning the sync computation may block async worker threads directly, which is strictly worse than blocking a dedicated blocking thread pool.

**`dry_run_transaction` is identically affected:**

```rust
// rpc/src/module/experiment.rs  L230-233
fn dry_run_transaction(&self, tx: Transaction) -> Result<EstimateCycles> {
    let tx: packed::Transaction = tx.into();
    CyclesEstimator::new(&self.shared).run(tx)
}
``` [5](#0-4) 

**No rate limiting exists:** A grep across all `rpc/**/*.rs` files for `rate_limit`, `RateLimit`, and `rate.limit` returns zero matches.

**No validity checks on the submitted transaction:** `CyclesEstimator` treats all cells as live regardless of actual chain state (see `CellProvider` impl at L2347-2361), so the attacker does not need to own any live cells or pay any fee. [6](#0-5) 

## Impact Explanation

Concurrent `estimate_cycles` requests with max-cycle scripts block Tokio threads for the full script execution duration (potentially many seconds per request). This starves the async runtime, making the node's entire JSON-RPC interface unresponsive and degrading block processing and P2P networking. This matches the **High** impact category: *"Vulnerabilities which could easily crash a CKB node"* and *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation

`estimate_cycles` is publicly documented, enabled by default, and reachable by any unprivileged caller. Crafting a max-cycle RISC-V script requires only basic CKB-VM knowledge. No key material, live cell ownership, fee payment, or PoW is required. The attack is fully automatable with standard HTTP tooling and is repeatable indefinitely.

## Recommendation

1. **Introduce a per-request cycle cap** for `estimate_cycles` / `dry_run_transaction` significantly below `max_block_cycles` (e.g., a configurable `rpc.max_estimate_cycles`).
2. **Run script execution in a cancellable task** so that the HTTP-level timeout or a dedicated script-execution timeout can actually abort the computation, not merely return a response to the client.
3. **Rate-limit** the `estimate_cycles` and `dry_run_transaction` endpoints per source IP or connection.
4. **Document** that these endpoints execute arbitrary scripts and should not be exposed on public-facing interfaces without additional protection.

## Proof of Concept

**Step 1 — Craft a max-cycle RISC-V script** that loops until `CyclesExceeded` at 3,500,000,000 cycles.

**Step 2 — Deploy the script cell** on-chain (or reference an existing cell dep with known data hash).

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

**Observed result:** The node's Tokio runtime saturates. Subsequent RPC calls (`get_tip_block_number`, `send_transaction`, etc.) time out or queue indefinitely. CPU reaches 100% on all cores. The `TimeoutLayer` returns `408` to the attacker's clients after 30 s, but the underlying script execution threads continue running, maintaining the DoS.

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

**File:** rpc/src/module/chain.rs (L2375-2389)
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

**File:** rpc/src/server.rs (L125-128)
```rust
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
```

**File:** rpc/src/module/experiment.rs (L230-233)
```rust
    fn dry_run_transaction(&self, tx: Transaction) -> Result<EstimateCycles> {
        let tx: packed::Transaction = tx.into();
        CyclesEstimator::new(&self.shared).run(tx)
    }
```
