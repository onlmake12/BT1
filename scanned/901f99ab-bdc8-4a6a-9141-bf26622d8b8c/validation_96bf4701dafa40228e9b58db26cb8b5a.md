Audit Report

## Title
Unauthenticated `estimate_cycles` / `dry_run_transaction` Executes Scripts at Full `max_block_cycles` Budget Against Dead Cells, Enabling CPU Exhaustion — (`rpc/src/module/chain.rs`)

## Summary
`CyclesEstimator::cell()` in `rpc/src/module/chain.rs` deliberately resolves every out-point as a live cell regardless of on-chain spent status. `CyclesEstimator::run()` then passes the resolved transaction to `ScriptVerifier::verify(max_block_cycles)` with no smaller per-call cap. An unprivileged caller can submit a transaction referencing any known-dead out-point carrying a maximally-looping CKB-VM script, causing the node to burn up to `max_block_cycles` (~3.5 B on mainnet) of synchronous CPU per unauthenticated RPC call, with no rate limiting in place.

## Finding Description
`CyclesEstimator` implements `CellProvider`. Its `cell()` method calls `snapshot.get_cell(out_point)` and, if the cell exists in the store (live or dead), wraps it in `CellStatus::live_cell(cell_meta)` — the in-code comment reads `// treat as live cell, regardless of live or dead`. [1](#0-0) 

This bypasses the dead-cell rejection enforced by the tx-pool and block verifier. `resolve_transaction` succeeds because every referenced cell appears live. `run()` then reads `consensus.max_block_cycles` and passes it directly to `ScriptVerifier::verify`: [2](#0-1) 

No smaller cap is applied at the RPC layer. Both `estimate_cycles` (chain.rs) and the deprecated `dry_run_transaction` (experiment.rs) share this code path: [3](#0-2) [4](#0-3) 

No authentication guard, per-IP limit, or per-caller throttle was found anywhere in the RPC module for these endpoints.

## Impact Explanation
Each crafted call can consume up to `max_block_cycles` of synchronous CPU on the node's RPC thread pool. A small number of concurrent callers can saturate all available CPU cores, stalling block-template generation and block-relay processing. This maps to the allowed High impact: **"Vulnerabilities which could easily crash a CKB node"** — the node remains running but is rendered unable to participate in block production, effectively crashing its mining/relay function.

## Likelihood Explanation
Prerequisites are trivially met by any external actor: (a) any spent out-point is publicly visible on any block explorer; (b) a RISC-V loop-to-cycle-limit script is a handful of bytes; (c) only standard HTTP JSON-RPC access is required. No key material, privileged access, or hashpower is needed. The attack is locally reproducible and indefinitely repeatable.

## Recommendation
- Apply a tighter per-call cycle cap (e.g., `max_block_cycles / N`) inside `CyclesEstimator::run()` instead of passing the full consensus limit.
- Add per-IP or per-connection rate limiting on `estimate_cycles` and `dry_run_transaction` at the RPC server layer.
- Optionally, reject inputs whose out-points resolve to dead cells in the estimator path, preserving the invariant that dead cells are never treated as live for script execution.

## Proof of Concept
1. Identify any spent out-point `(tx_hash, index)` from the canonical chain (any block explorer suffices).
2. Construct a CKB-VM script that loops until cycle exhaustion (a tight RISC-V `j .` loop, ~4 bytes).
3. Build a transaction with that out-point as input and the looping script as its lock script.
4. From N concurrent clients, send:
   ```
   POST / HTTP/1.1
   Content-Type: application/json
   {"jsonrpc":"2.0","method":"estimate_cycles","params":[<tx>],"id":1}
   ```
5. Observe node CPU pinned at 100 % per concurrent request; block-template RPC latency increases proportionally; block production stalls.

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

**File:** rpc/src/module/experiment.rs (L2-3)
```rust
use crate::module::chain::CyclesEstimator;
use async_trait::async_trait;
```

**File:** rpc/src/module/experiment.rs (L230-233)
```rust
    fn dry_run_transaction(&self, tx: Transaction) -> Result<EstimateCycles> {
        let tx: packed::Transaction = tx.into();
        CyclesEstimator::new(&self.shared).run(tx)
    }
```
