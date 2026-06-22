### Title
Unbounded CKB-VM Cycle Execution in `estimate_cycles` / `dry_run_transaction` RPC — (File: `rpc/src/module/chain.rs`)

---

### Summary

The `estimate_cycles` RPC (and its deprecated alias `dry_run_transaction`) executes arbitrary CKB-VM scripts synchronously on the RPC handler thread using `consensus.max_block_cycles` as the cycle ceiling — the same budget allocated to an *entire block* — with no per-query cycle cap, no fee requirement, and no rate limiting. Any unprivileged RPC caller can submit a crafted transaction whose scripts consume up to 3.5 billion cycles per call, exhausting CPU resources and starving the node of RPC capacity.

---

### Finding Description

`CyclesEstimator::run` in `rpc/src/module/chain.rs` resolves and fully verifies the scripts of a caller-supplied transaction, passing `consensus.max_block_cycles` directly as the `max_cycles` argument to `ScriptVerifier::verify`:

```rust
// rpc/src/module/chain.rs  CyclesEstimator::run
let max_cycles = consensus.max_block_cycles;   // ← full block budget, e.g. 3 500 000 000
...
ScriptVerifier::new(Arc::new(resolved), snapshot.as_data_loader(), consensus, Arc::new(tx_env))
    .verify(max_cycles)
``` [1](#0-0) 

`ScriptVerifier::verify` iterates every script group and calls `verify_script_group(group, max_cycles - cycles)`, running the CKB-VM scheduler until the script exits or the cycle budget is exhausted: [2](#0-1) 

The deprecated `dry_run_transaction` in `rpc/src/module/experiment.rs` delegates to the same `CyclesEstimator::run` path: [3](#0-2) 

The tx-pool's normal transaction admission path (`_process_tx`) also uses `consensus.max_block_cycles()` as the fallback when no declared cycles are provided, but the tx-pool enforces an additional `max_tx_verify_cycles` admission gate (configured at 70 000 000 cycles by default). `estimate_cycles` bypasses the tx-pool entirely and applies only the much larger block-level ceiling, giving a single query up to **50× more execution budget** than a normal submitted transaction. [4](#0-3) 

---

### Impact Explanation

An attacker deploys a script that loops for close to `max_block_cycles` cycles (≈ 3.5 billion on mainnet). Calling `estimate_cycles` with a transaction referencing that script causes the RPC handler thread to execute the CKB-VM for the full duration of the script — potentially several seconds per call. Flooding the node with concurrent `estimate_cycles` requests saturates all RPC worker threads, making the node unresponsive to legitimate RPC calls (`send_transaction`, `get_block_template`, etc.) and degrading block-relay and tx-pool operations that share the same process.

---

### Likelihood Explanation

- `estimate_cycles` is part of the default `Chain` RPC module, enabled on every full node.
- No authentication, fee, or stake is required.
- The attacker only needs to deploy one computationally expensive script (or reference an existing one already on-chain) and issue repeated HTTP POST requests.
- The attack is cheap, repeatable, and requires no special privilege.

---

### Recommendation

1. Introduce a dedicated per-query cycle cap (e.g., `max_estimate_cycles` in `[rpc]` config, defaulting to `max_tx_verify_cycles` or a similarly conservative value) and apply it inside `CyclesEstimator::run` instead of `consensus.max_block_cycles`.
2. Run `estimate_cycles` / `dry_run_transaction` in a separate thread pool with bounded concurrency so that long-running script executions cannot starve the main RPC worker pool.
3. Consider adding optional RPC-level rate limiting for script-executing endpoints.

---

### Proof of Concept

1. Write a RISC-V script that spins in a tight loop for ~3 000 000 000 cycles and deploy it to a CKB testnet cell.
2. Construct a transaction whose lock script references that cell.
3. Send the following request repeatedly (e.g., 20 concurrent connections):

```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "estimate_cycles",
  "params": [{ "cell_deps": [...], "inputs": [...], "outputs": [...], "outputs_data": ["0x"], "version": "0x0", "witnesses": [] }]
}
```

4. Observe that all RPC threads are occupied executing the script; subsequent `get_tip_block_number` or `send_transaction` calls time out or queue indefinitely, demonstrating node-level RPC denial of service. [1](#0-0) [5](#0-4)

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

**File:** rpc/src/module/experiment.rs (L99-104)
```rust
    #[deprecated(
        since = "0.105.1",
        note = "Please use the RPC method [`estimate_cycles`](#chain-estimate_cycles) instead"
    )]
    #[rpc(name = "dry_run_transaction")]
    fn dry_run_transaction(&self, tx: Transaction) -> Result<EstimateCycles>;
```

**File:** rpc/src/module/experiment.rs (L230-233)
```rust
    fn dry_run_transaction(&self, tx: Transaction) -> Result<EstimateCycles> {
        let tx: packed::Transaction = tx.into();
        CyclesEstimator::new(&self.shared).run(tx)
    }
```

**File:** tx-pool/src/process.rs (L720-732)
```rust
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;
```
