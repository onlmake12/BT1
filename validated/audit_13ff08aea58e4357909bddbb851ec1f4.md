Audit Report

## Title
Unbounded CKB-VM Cycle Execution in `estimate_cycles` / `dry_run_transaction` RPC — (File: `rpc/src/module/chain.rs`)

## Summary
`CyclesEstimator::run` in `rpc/src/module/chain.rs` executes arbitrary CKB-VM scripts synchronously on the RPC handler thread using `consensus.max_block_cycles` (≈3.5 billion on mainnet) as the sole cycle ceiling, with no per-query cap, no fee requirement, and no rate limiting. An unprivileged caller can submit a crafted transaction whose scripts consume the full block cycle budget per call, saturating all RPC worker threads and rendering the node unresponsive. The deprecated `dry_run_transaction` in `rpc/src/module/experiment.rs` is identically affected via the same code path.

## Finding Description
`CyclesEstimator::run` at `rpc/src/module/chain.rs:2380` reads `consensus.max_block_cycles` and passes it directly to `ScriptVerifier::verify`:

```rust
let max_cycles = consensus.max_block_cycles;
...
ScriptVerifier::new(Arc::new(resolved), snapshot.as_data_loader(), consensus, Arc::new(tx_env))
    .verify(max_cycles)
```

`ScriptVerifier::verify` at `script/src/verify.rs:197–214` iterates every script group and calls `verify_script_group(group, max_cycles - cycles)`, running the CKB-VM scheduler synchronously until the script exits or the budget is exhausted. There is no timeout, no concurrency limit, and no intermediate yield point visible to the RPC layer.

`dry_run_transaction` in `rpc/src/module/experiment.rs:230–233` delegates unconditionally to the same `CyclesEstimator::new(&self.shared).run(tx)` call, so both endpoints share the same unbounded execution path.

The tx-pool's `_process_tx` at `tx-pool/src/process.rs:720` also falls back to `self.consensus.max_block_cycles()` when no declared cycles are provided, but the tx-pool path runs through `verify_rtx` (in `tx-pool/src/util.rs`) which accepts a `max_tx_verify_cycles` parameter that can be bounded by the `TxPoolConfig::max_tx_verify_cycles` field (default 70,000,000 cycles). `estimate_cycles` bypasses the tx-pool entirely and applies only the much larger block-level ceiling, giving a single RPC query up to ~50× more execution budget than a normal submitted transaction.

## Impact Explanation
An attacker who deploys a script that loops for close to `max_block_cycles` cycles (or references an existing expensive script already on-chain) can issue concurrent `estimate_cycles` HTTP POST requests. Each request occupies one RPC worker thread for the full duration of the script execution. With enough concurrent requests, all RPC worker threads are saturated, making the node unresponsive to `send_transaction`, `get_block_template`, and other RPC calls. Because block template generation and transaction relay depend on RPC availability within the same process, sustained saturation degrades the node's ability to participate in block production and propagation. Applied simultaneously to multiple nodes, this constitutes network-wide congestion with negligible attacker cost. This matches the **High** impact: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs* and *Vulnerabilities which could easily crash a CKB node*.

## Likelihood Explanation
- `estimate_cycles` is part of the default `Chain` RPC module, enabled on every full node with no authentication.
- No fee, stake, or special privilege is required.
- The attacker only needs to deploy one expensive script once (or reuse an existing on-chain script) and issue repeated HTTP POST requests.
- The attack is cheap, stateless, and trivially parallelizable from a single machine.

## Recommendation
1. Introduce a dedicated per-query cycle cap (e.g., `max_estimate_cycles` in `[rpc]` config, defaulting to `TxPoolConfig::max_tx_verify_cycles` or a similarly conservative value) and apply it inside `CyclesEstimator::run` instead of `consensus.max_block_cycles`.
2. Execute `estimate_cycles` / `dry_run_transaction` in a separate, bounded thread pool so that long-running script executions cannot starve the main RPC worker pool.
3. Consider optional RPC-level rate limiting for script-executing endpoints.

## Proof of Concept
1. Write a RISC-V script that spins in a tight loop for ~3,000,000,000 cycles and deploy it to a CKB testnet cell.
2. Construct a transaction whose lock script references that cell.
3. Send the following request with 20+ concurrent connections:
```json
{
  "id": 1, "jsonrpc": "2.0", "method": "estimate_cycles",
  "params": [{ "cell_deps": [...], "inputs": [...], "outputs": [...],
               "outputs_data": ["0x"], "version": "0x0", "witnesses": [] }]
}
```
4. Observe that all RPC threads are occupied executing the script; subsequent `get_tip_block_number` or `send_transaction` calls time out or queue indefinitely, demonstrating node-level RPC denial of service. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** rpc/src/module/experiment.rs (L230-233)
```rust
    fn dry_run_transaction(&self, tx: Transaction) -> Result<EstimateCycles> {
        let tx: packed::Transaction = tx.into();
        CyclesEstimator::new(&self.shared).run(tx)
    }
```

**File:** tx-pool/src/util.rs (L85-132)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
}
```

**File:** util/app-config/src/configs/tx_pool.rs (L20-22)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
```
