Audit Report

## Title
Unauthenticated `estimate_cycles` Executes Scripts at Full `max_block_cycles` Budget with No Rate Limiting, Enabling Node DoS — (File: `rpc/src/module/chain.rs`)

## Summary
`CyclesEstimator::run()` executes attacker-supplied scripts up to `consensus.max_block_cycles` (3.5 billion cycles on mainnet) with no authentication, no per-call cycle cap, and no rate limiting. The `CellProvider` implementation unconditionally returns `CellStatus::live_cell` for any cell — including already-spent (dead) cells — allowing an attacker to reference historical cells they no longer own to trigger expensive script execution. An unprivileged attacker can flood this endpoint to saturate the node's script-execution workers and render the node unresponsive.

## Finding Description

`estimate_cycles` is declared in the `Chain` RPC trait and implemented without any access control: [1](#0-0) 

`CyclesEstimator::run()` sets the execution limit to the full block cycle budget: [2](#0-1) 

The `CellProvider` implementation explicitly treats every cell — live or dead — as live, with the comment confirming this is intentional: [3](#0-2) 

The RPC `Config` struct has no per-method rate limiting, no authentication fields, and no per-endpoint cycle cap: [4](#0-3) 

The same `CyclesEstimator::run()` path is also reachable via the deprecated `dry_run_transaction` in the `Experiment` module: [5](#0-4) 

The exploit chain is:
1. Deploy a RISC-V script that loops for `max_block_cycles` cycles and let the cell be spent (dead).
2. Craft a transaction whose input references the now-dead cell. Because `CellProvider::cell()` returns `CellStatus::live_cell` unconditionally for any cell found in the snapshot, `resolve_transaction` succeeds.
3. Call `estimate_cycles` in a tight loop from multiple connections. Each call invokes `ScriptVerifier::verify(max_block_cycles)`, occupying a worker thread for the full execution duration.
4. The node's RPC thread pool and async runtime are saturated; block template generation, transaction relay, and chain sync responses are degraded or halted.

No existing guard prevents this: there is no cycle cap below `max_block_cycles`, no request queue depth limit per method, and no authentication requirement on the `Chain` module.

## Impact Explanation

This is a **High** severity issue matching: *"Vulnerabilities which could easily crash a CKB node"* and *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

A fully saturated script-execution worker pool causes the node to stop responding to all RPC requests (block template, tx relay, sync), effectively crashing it from a service perspective. The attacker does not need to crash the process — making the node unresponsive to the network achieves the same result. The "dead cells treated as live" aspect means the attacker needs zero live on-chain assets to sustain the attack indefinitely.

## Likelihood Explanation

- The `Chain` RPC module is standard and default-enabled on public CKB nodes.
- No fee, no signature, no on-chain asset ownership is required.
- The attack is repeatable indefinitely at negligible cost (only network bandwidth).
- `max_block_cycles` on mainnet is 3,500,000,000 — a single call can occupy a worker for multiple seconds.
- The dead-cell bypass removes the only natural economic friction (needing to hold a live cell with an expensive script).

## Recommendation

1. **Impose a per-call cycle cap** in `CyclesEstimator::run()` significantly lower than `max_block_cycles` (e.g., `max_tx_verify_cycles` or a dedicated `estimate_cycles_limit` consensus parameter).
2. **Reject dead cells** in `CyclesEstimator`'s `CellProvider`: check whether the cell is live before returning `CellStatus::live_cell`, consistent with how the tx-pool's `CellProvider` behaves.
3. **Add rate limiting** at the RPC server layer for compute-heavy endpoints (`estimate_cycles`, `dry_run_transaction`, `test_tx_pool_accept`), e.g., a per-IP or global request queue depth limit.
4. Consider requiring a small proof-of-work or fee commitment before executing scripts via the estimation endpoint.

## Proof of Concept

```bash
# 1. Compile a RISC-V ELF that loops for max_block_cycles and deploy it on-chain.
# 2. Spend the cell containing that script (making it a dead cell).
# 3. Craft a transaction whose input references the now-spent cell.
# 4. Flood estimate_cycles:

while true; do
  curl -s -X POST http://<node>:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"estimate_cycles","params":[<crafted_tx_json>],"id":1}' &
done
```

Each concurrent call invokes `ScriptVerifier::verify(max_block_cycles)` on a worker thread. Because `CellProvider::cell()` returns `live_cell` for the spent cell unconditionally, `resolve_transaction` succeeds and script execution proceeds to the full cycle limit. With enough concurrent calls, all worker threads are occupied and the node becomes unresponsive to legitimate traffic. [6](#0-5)

### Citations

**File:** rpc/src/module/chain.rs (L2119-2122)
```rust
    fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles> {
        let tx: packed::Transaction = tx.into();
        CyclesEstimator::new(&self.shared).run(tx)
    }
```

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

**File:** util/app-config/src/configs/rpc.rs (L23-61)
```rust
/// RPC config options.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Eq)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// RPC server listen addresses.
    pub listen_address: String,
    /// RPC TCP server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub tcp_listen_address: Option<String>,
    /// RPC WS server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub ws_listen_address: Option<String>,
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
    /// Number of RPC worker threads.
    pub threads: Option<usize>,
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
    /// Enabled RPC modules.
    pub modules: Vec<Module>,
    /// Rejects txs with scripts that might trigger known bugs
    #[serde(default)]
    pub reject_ill_transactions: bool,
    /// Whether enable deprecated RPC methods.
    ///
    /// Deprecated RPC methods are disabled by default.
    #[serde(default)]
    pub enable_deprecated_rpc: bool,
    /// Customized extra well known lock scripts.
    #[serde(default)]
    pub extra_well_known_lock_scripts: Vec<Script>,
    /// Customized extra well known type scripts.
    #[serde(default)]
    pub extra_well_known_type_scripts: Vec<Script>,
}
```

**File:** rpc/src/module/experiment.rs (L230-233)
```rust
    fn dry_run_transaction(&self, tx: Transaction) -> Result<EstimateCycles> {
        let tx: packed::Transaction = tx.into();
        CyclesEstimator::new(&self.shared).run(tx)
    }
```
