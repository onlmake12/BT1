### Title
`_test_accept_tx` Uses `max_block_cycles` Instead of `max_tx_verify_cycles`, Enabling CPU Exhaustion DoS — (File: `tx-pool/src/process.rs`)

### Summary

The `test_accept_tx` RPC endpoint invokes `_test_accept_tx`, which sets the script execution cycle limit to `consensus.max_block_cycles()` (3.5 billion cycles) rather than the operator-configured `tx_pool_config.max_tx_verify_cycles` (default 70 million cycles). This is a 50× discrepancy. Because the verification runs via `block_in_place` (synchronous blocking), any local RPC caller can submit a crafted transaction whose script consumes up to 3.5B cycles, occupying a blocking worker thread for an extended period. Repeated calls exhaust the blocking thread pool and degrade or halt node responsiveness.

---

### Finding Description

**Root cause — wrong cycle cap in `_test_accept_tx`:** [1](#0-0) 

At line 787, the cycle limit is unconditionally set to `self.consensus.max_block_cycles()`:

```rust
let max_cycles = self.consensus.max_block_cycles();   // 3_500_000_000
```

The operator-configured per-transaction limit lives in `tx_pool_config.max_tx_verify_cycles` (default 70,000,000): [2](#0-1) 

The same `max_tx_verify_cycles` is correctly applied in `readd_detached_tx`: [3](#0-2) 

But `_test_accept_tx` never consults it.

**Execution path — synchronous blocking:**

`_test_accept_tx` calls `verify_rtx` with `command_rx = None`: [4](#0-3) 

The `command_rx = None` branch uses `block_in_place`, which occupies a blocking worker thread for the full duration of script execution. With `max_block_cycles = 3.5B`, a tight-loop script can hold that thread for seconds per call.

**Entry point — Pool RPC, enabled by default:**

`test_accept_tx` is part of the `Pool` module, which is enabled in the default configuration: [5](#0-4) 

Any local RPC caller (a "supported local CLI/RPC user") can reach it without any privileged key.

**Contrast with the async verify-queue path:**

Remote transactions go through the verify queue, which correctly caps cycles at `max_tx_verify_cycles` and uses the async `verify_with_pause` path (non-blocking). `test_accept_tx` bypasses both protections. [6](#0-5) 

---

### Impact Explanation

A local RPC caller submits a transaction whose lock script runs a tight loop consuming ~3.5B cycles. Each `test_accept_tx` call:

1. Blocks a `block_in_place` worker thread for the full verification duration (~50× longer than the configured limit).
2. Repeated concurrent calls exhaust the blocking thread pool.
3. The tokio runtime queues further blocking work, stalling RPC responses, block processing callbacks, and other node services that share the runtime.

Result: sustained CPU exhaustion and effective node DoS, without inserting any transaction into the pool or the chain.

---

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, so the attacker must have local shell access to the node machine. This matches the "supported local CLI/RPC user" attacker profile explicitly listed in scope. No privileged keys, no network access, and no special permissions beyond the ability to make localhost HTTP requests are required. The `Pool` module is on by default, and `test_accept_tx` is a documented, stable RPC method.

---

### Recommendation

Replace the hardcoded `max_block_cycles` cap in `_test_accept_tx` with the configured per-transaction limit:

```rust
// tx-pool/src/process.rs  _test_accept_tx
- let max_cycles = self.consensus.max_block_cycles();
+ let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
```

This aligns `test_accept_tx` with the same resource budget enforced for all other transaction verification paths.

---

### Proof of Concept

1. Compile a RISC-V lock script that executes a tight loop for ~3.4B cycles (just under `max_block_cycles`).
2. Place it in a cell dep and construct a transaction whose input uses it as a lock script.
3. Call the RPC endpoint:
   ```json
   {"jsonrpc":"2.0","method":"test_accept_tx","params":[{"version":"0x0","cell_deps":[...],"header_deps":[],"inputs":[...],"outputs":[],"outputs_data":[],"witnesses":[]}],"id":1}
   ```
4. Observe that the call blocks for several seconds (vs. ~20 ms for a 70M-cycle script).
5. Issue 8–16 concurrent calls to saturate the blocking thread pool; subsequent RPC calls and internal node tasks stall.

The discrepancy is deterministic: `max_block_cycles = TWO_IN_TWO_OUT_CYCLES × 1000 = 3,500,000,000` vs. `max_tx_verify_cycles = 70,000,000` — a fixed 50× gap that any caller can exploit on every invocation. [7](#0-6) [8](#0-7)

### Citations

**File:** tx-pool/src/process.rs (L591-628)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
            for orphan in orphans.into_iter() {
                if orphan.cycle > self.tx_pool_config.max_tx_verify_cycles {
                    debug!(
                        "process_orphan {} added to verify queue; find previous from {}",
                        orphan.tx.hash(),
                        tx.hash(),
                    );
                    let orphan_id = orphan.tx.proposal_short_id();
                    match self
                        .enqueue_verify_queue(
                            orphan.tx.clone(),
                            false,
                            Some((orphan.cycle, orphan.peer)),
                        )
                        .await
                    {
                        Ok(_) => {
                            self.remove_orphan_tx(&orphan_id).await;
                        }
                        Err(reject) => {
                            warn!(
                                "process_orphan {} failed to enqueue verify queue: {}; keep orphan from {}",
                                orphan.tx.hash(),
                                reject,
                                tx.hash(),
                            );
                        }
                    }
                } else if let Some((ret, _snapshot)) = self
                    ._process_tx(orphan.tx.clone(), Some(orphan.cycle), None)
                    .await
                {
```

**File:** tx-pool/src/process.rs (L779-800)
```rust
    pub(crate) async fn _test_accept_tx(&self, tx: TransactionView) -> Result<Completed, Reject> {
        let (pre_check_ret, snapshot) = self.pre_check(&tx).await;

        let (_tip_hash, rtx, status, _fee, _tx_size) = pre_check_ret?;

        // skip check the delay window

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = self.consensus.max_block_cycles();
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            None,
        )
        .await
    }
```

**File:** tx-pool/src/process.rs (L884-884)
```rust
        let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
```

**File:** resource/ckb.toml (L190-190)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
```

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
```

**File:** tx-pool/src/util.rs (L116-131)
```rust
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
```

**File:** spec/src/consensus.rs (L84-84)
```rust
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```
