### Title
Missing Cell Liveness Prerequisite Check Before Script Execution in `estimate_cycles` RPC Enables CPU Exhaustion DoS - (File: `rpc/src/module/chain.rs`)

### Summary

The `estimate_cycles` RPC endpoint uses a `CyclesEstimator` cell provider that explicitly bypasses the cell liveness prerequisite check, treating dead (already-spent) cells as live before running the full `ScriptVerifier`. Any unprivileged RPC caller can submit a transaction referencing dead cells with maximally expensive scripts, causing the node to execute scripts up to `consensus.max_block_cycles` cycles per call with no liveness gate, creating a CPU exhaustion DoS path.

### Finding Description

The `CyclesEstimator` struct in `rpc/src/module/chain.rs` implements `CellProvider` as follows:

```rust
fn cell(&self, out_point: &packed::OutPoint, eager_load: bool) -> CellStatus {
    let snapshot = self.shared.snapshot();
    snapshot
        .get_cell(out_point)
        .map(|mut cell_meta| {
            // ...
            CellStatus::live_cell(cell_meta)
        })  // treat as live cell, regardless of live or dead
        .unwrap_or(CellStatus::Unknown)
}
```

The comment is explicit: dead cells are promoted to `CellStatus::live_cell`. This means `resolve_transaction` succeeds for transactions whose inputs reference already-spent cells. The resolved transaction is then passed directly to `ScriptVerifier::new(...).verify(max_cycles)` where `max_cycles = consensus.max_block_cycles` — the consensus-wide maximum.

The full call chain in `CyclesEstimator::run`:

1. `resolve_transaction(tx.into_view(), &mut HashSet::new(), self, self)` — resolves using the liveness-bypassing provider; succeeds for dead-cell inputs.
2. `ScriptVerifier::new(Arc::new(resolved), ...).verify(max_cycles)` — executes all lock and type scripts up to the full block cycle budget.

In every other code path (tx-pool admission, block verification), the prerequisite check is enforced: `CellChecker::is_live` returns `Some(false)` for dead cells and the operation is rejected before script execution begins. `CyclesEstimator` is the sole path that skips this gate.

### Impact Explanation

An unprivileged RPC caller can:

1. Identify any dead out-point on-chain (trivially observable from chain history).
2. Craft a transaction whose inputs reference those dead cells and whose lock/type scripts perform maximally expensive computation (e.g., a script that loops until cycle limit).
3. Call `estimate_cycles` repeatedly in parallel.

Each call drives the node's CPU to execute scripts for up to `max_block_cycles` cycles synchronously on the RPC handler thread. Because the liveness check is absent, the node cannot distinguish this from a legitimate estimation request and cannot reject it early. The result is sustained CPU exhaustion, degrading or halting block processing, P2P message handling, and other RPC responses on the same node.

### Likelihood Explanation

- The `estimate_cycles` RPC is publicly documented and reachable by any client with RPC access (default: localhost, but commonly exposed by node operators for tooling).
- Dead out-points are trivially enumerable from chain history; no privileged knowledge is required.
- No authentication, rate limiting, or cycle budget cap specific to `estimate_cycles` exists beyond the global `max_block_cycles` consensus constant.
- The attack requires only standard RPC calls and publicly available chain data.

### Recommendation

Before invoking `resolve_transaction` in `CyclesEstimator::run`, verify that all input out-points are live using the snapshot's authoritative liveness check (the same `CellChecker` path used by tx-pool and block verification). Alternatively, implement a per-call cycle cap for `estimate_cycles` that is substantially lower than `max_block_cycles`, and enforce rate limiting on the endpoint. The cell provider should not silently promote dead cells to live status; it should return `CellStatus::Dead` for spent cells, causing `resolve_transaction` to return an error before any script execution occurs.

### Proof of Concept

1. Submit any valid transaction `T` to the chain and wait for it to be confirmed. Its output at index 0 is now a dead cell (out-point `(T.hash, 0)`).
2. Construct a new transaction `T2` with:
   - `inputs[0].previous_output = (T.hash, 0)` (dead cell)
   - A lock script that loops until cycle exhaustion (e.g., an infinite loop script deployed as a cell dep).
3. Call `estimate_cycles` with `T2` via JSON-RPC.
4. Observe that the node executes the script up to `max_block_cycles` cycles despite the input being dead, consuming full CPU for the duration.
5. Repeat in a tight loop from multiple connections to sustain CPU exhaustion.

The root cause is at: [1](#0-0) 

where `CellStatus::live_cell(cell_meta)` is returned unconditionally for any cell found in the snapshot, with the explicit comment "treat as live cell, regardless of live or dead," and at: [2](#0-1) 

where `ScriptVerifier::new(...).verify(max_cycles)` is called with `max_cycles = consensus.max_block_cycles` on the resolved (dead-cell-promoted) transaction with no prior liveness gate.

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
