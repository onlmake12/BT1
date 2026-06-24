Audit Report

## Title
Missing Cell Liveness Check in `CyclesEstimator` Enables Unauthenticated CPU Exhaustion DoS via `estimate_cycles` RPC - (File: `rpc/src/module/chain.rs`)

## Summary

The `CyclesEstimator` struct in `rpc/src/module/chain.rs` implements `CellProvider` such that any cell found in the snapshot is unconditionally promoted to `CellStatus::live_cell`, regardless of whether it has already been spent. This allows any caller with RPC access to submit transactions referencing trivially-enumerable dead cells paired with maximally expensive scripts, causing the node to execute scripts up to `consensus.max_block_cycles` cycles per call with no early rejection, no rate limiting, and no cycle cap specific to this endpoint.

## Finding Description

The `CyclesEstimator::cell` implementation at `rpc/src/module/chain.rs:2347-2361` returns `CellStatus::live_cell(cell_meta)` for every cell found in the snapshot. The inline comment is explicit: "treat as live cell, regardless of live or dead." No `CellStatus::Dead` path exists in this provider. [1](#0-0) 

As a result, `resolve_transaction` in `CyclesEstimator::run` succeeds for inputs referencing already-spent out-points. The resolved transaction is then passed directly to `ScriptVerifier::new(...).verify(max_cycles)` where `max_cycles = consensus.max_block_cycles` — the consensus-wide maximum, with no per-endpoint cap. [2](#0-1) 

In every other code path (tx-pool admission, block verification), a liveness gate rejects dead-cell inputs before script execution begins. `CyclesEstimator` is the sole path that skips this gate. No rate limiting was found anywhere in the RPC layer for this endpoint.

The liveness bypass is the critical enabler: without it, an attacker would need to own live cells with expensive lock scripts (requiring on-chain CKB tokens). With it, any dead out-point from chain history (freely observable, no tokens required) suffices as the input, and any existing expensive script deployed as a cell dep can be referenced.

## Impact Explanation

An attacker with RPC access can drive the node's CPU to execute scripts for up to `max_block_cycles` cycles synchronously on the RPC handler thread, per call, with no early rejection. Repeated parallel calls cause sustained CPU exhaustion, degrading or halting block processing, P2P message handling, and other RPC responses. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

- `estimate_cycles` is publicly documented and commonly exposed by node operators for tooling (default localhost, but frequently opened).
- Dead out-points are trivially enumerable from chain history; no privileged knowledge or CKB tokens are required.
- No authentication, rate limiting, or per-call cycle cap exists beyond the global `max_block_cycles` consensus constant.
- The attack requires only standard JSON-RPC calls and publicly available chain data, executable from a single script in a tight loop.

## Recommendation

In `CyclesEstimator::cell`, check whether the cell is live before returning `CellStatus::live_cell`. The snapshot's authoritative liveness check (the same path used by tx-pool and block verification) should be used: return `CellStatus::Dead` for spent cells so that `resolve_transaction` rejects the transaction before any script execution occurs. Additionally, enforce a per-call cycle cap for `estimate_cycles` substantially lower than `max_block_cycles`, and apply rate limiting on the endpoint.

## Proof of Concept

1. Submit any valid transaction `T` to the chain and wait for confirmation. Its output at index 0 is now a dead cell (out-point `(T.hash, 0)`).
2. Identify or deploy any cell dep containing a script that loops until cycle exhaustion.
3. Construct transaction `T2` with `inputs[0].previous_output = (T.hash, 0)` and the expensive script as its lock, referencing the loop script via cell dep.
4. Call `estimate_cycles` with `T2` via JSON-RPC. Observe the node executes the script up to `max_block_cycles` cycles despite the input being dead.
5. Repeat in a tight loop from multiple connections. Observe sustained CPU exhaustion and degraded node responsiveness.

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
