### Title
Script Authors Can Force Maximum-Cycle Execution Against Verifying Nodes via Size-Only Fee Pre-Check — (File: `tx-pool/src/util.rs`, `tx-pool/src/process.rs`)

---

### Summary

CKB's tx-pool fee pre-check computes the minimum required fee using only the transaction's serialized byte size, while the actual computational cost of script execution is measured in cycles. A script author can deploy a lock or type script that consumes up to `max_block_cycles` cycles, submit a small transaction (low byte size, low fee), pass the size-based fee gate, and force every verifying node to execute the script for the full declared cycle budget. This is the direct CKB analog of gas griefing: an attacker-controlled code path is invoked during a protocol action (tx-pool admission) and can consume the maximum allowed computational resource at below-cost fee.

---

### Finding Description

**Root cause — fee pre-check ignores cycles:**

In `tx-pool/src/util.rs` at line 45, `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The comment at line 42–44 explicitly acknowledges this is a deliberate approximation:

```
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
``` [1](#0-0) 

The actual transaction weight used for pool prioritization is defined in `util/types/src/core/tx_pool.rs`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` and `max_block_cycles ≈ 3.5 × 10⁹`, the weight of a maximum-cycle transaction is approximately 597,000 bytes, requiring ~597,000 shannons at the default 1,000 shannons/KB rate. A 1 KB transaction passes the pre-check with only ~1,000 shannons — a 597× underpayment for the actual computational cost. [3](#0-2) 

**Root cause — declared_cycles used as execution limit:**

In `tx-pool/src/process.rs` at line 720, the `max_cycles` passed to script execution is set to `declared_cycles` (for remote transactions) or `max_block_cycles` (for local transactions):

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

This value is then passed directly to `verify_rtx`, which runs the CKB-VM scheduler up to that cycle limit: [4](#0-3) 

The only upstream guard on `declared_cycles` in `sync/src/relayer/transactions_process.rs` is that it must not exceed `max_block_cycles`:

```rust
if txs.iter().any(|(_, declared_cycles)| declared_cycles > &max_block_cycles) {
    self.nc.ban_peer(...);
}
``` [5](#0-4) 

There is no check against `max_tx_verify_cycles` at this relay-admission stage. Scripts are executed for up to `declared_cycles` cycles before any weight-based fee adequacy check can occur.

**Execution path:**

`TransactionsProcess::execute` → `tx_pool.submit_remote_tx(tx, declared_cycles, peer)` → `_process_tx` → `check_tx_fee(tx_size)` [passes] → `verify_rtx(max_cycles = declared_cycles)` [scripts run] → `DeclaredWrongCycles` or pool admission. [6](#0-5) [7](#0-6) 

The `Scheduler::run` in `script/src/scheduler.rs` enforces the cycle limit inside the VM loop, meaning the VM executes until it either terminates or exhausts `limit_cycles`: [8](#0-7) 

---

### Impact Explanation

A script author deploys a lock or type script that consumes exactly `N` cycles (up to `max_block_cycles`) and returns exit code 0. Any transaction spending a cell locked by this script forces every receiving node to execute the script for `N` cycles during tx-pool admission. Because the fee pre-check uses only byte size, the attacker pays the minimum fee for a small transaction (~1,000 shannons for 1 KB) while forcing up to `max_block_cycles` cycles of CPU work per node per transaction submission. Repeated submissions across many peers constitute a sustained CPU-exhaustion attack against the network's tx-pool workers, degrading transaction throughput and potentially causing legitimate transactions to be delayed or dropped.

---

### Likelihood Explanation

Any unprivileged actor who can deploy a script cell and submit or relay a transaction can trigger this path. No privileged keys, majority hashpower, or Sybil capability is required. The attacker only needs to pay the minimum size-based fee. The attack is repeatable and scales with the number of connected peers willing to relay the transaction.

---

### Recommendation

1. **Pre-check fee against declared cycles**: Before executing scripts, compute `weight = max(tx_size, declared_cycles * DEFAULT_BYTES_PER_CYCLES)` and require `fee ≥ min_fee_rate.fee(weight)`. This ensures the fee covers the declared computational cost before any VM execution occurs.
2. **Enforce `max_tx_verify_cycles` at relay admission**: In `TransactionsProcess::execute`, reject (and ban) peers whose `declared_cycles` exceeds `max_tx_verify_cycles`, not just `max_block_cycles`. This caps the per-transaction CPU exposure at the pool's configured limit.
3. **Rate-limit script execution workers per peer**: Bound the number of concurrent or queued verifications attributable to a single peer to prevent a single source from saturating the verify queue.

---

### Proof of Concept

1. Deploy a RISC-V lock script that spins in a tight loop consuming exactly `C` cycles (e.g., `C = 70_000_000`, the default `max_tx_verify_cycles`) and returns exit code 0.
2. Create a live cell on-chain locked by this script.
3. Construct a transaction spending that cell. Serialized size: ~200 bytes. Required fee at 1,000 shannons/KB: ~200 shannons.
4. Relay the transaction to a target node via `RelayTransactions` with `declared_cycles = C`.
5. The node's `check_tx_fee` passes (200 shannons ≥ 200 shannons minimum for 200 bytes).
6. `verify_rtx` is called with `max_cycles = C`; the VM executes for `C` cycles.
7. The script exits with code 0; `verified.cycles == declared_cycles`; the transaction is admitted to the pool.
8. Repeat from step 3 with a new input (or use RBF) to continuously force `C`-cycle executions on the node.

The attacker pays ~200 shannons per forced `C`-cycle execution. At `C = 70_000_000` cycles, the computational cost to the node is orders of magnitude larger than the fee paid, constituting a resource-accounting imbalance directly analogous to gas griefing in the referenced EVM delegation report. [9](#0-8) [10](#0-9) [5](#0-4) [3](#0-2)

### Citations

**File:** tx-pool/src/util.rs (L28-53)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
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

**File:** util/types/src/core/tx_pool.rs (L276-303)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

/// vbytes has been deprecated, renamed to weight to prevent ambiguity
#[deprecated(
    since = "0.107.0",
    note = "Please use the get_transaction_weight instead"
)]
pub fn get_transaction_virtual_bytes(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}

/// The miners select transactions to fill the limited block space which gives the highest fee.
/// Because there are two different limits, serialized size and consumed cycles,
/// the selection algorithm is a multi-dimensional knapsack problem.
/// Introducing the transaction weight converts the multi-dimensional knapsack to a typical knapsack problem,
/// which has a simple greedy algorithm.
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/process.rs (L705-751)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
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

        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

**File:** sync/src/relayer/transactions_process.rs (L63-74)
```rust
        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }
```

**File:** script/src/scheduler.rs (L291-305)
```rust
    pub fn run(&mut self, mode: RunMode) -> Result<TerminatedResult, Error> {
        self.boot_root_vm_if_needed()?;

        let (pause, mut limit_cycles) = match mode {
            RunMode::LimitCycles(limit_cycles) => (Pause::new(), limit_cycles),
            RunMode::Pause(pause, limit_cycles) => (pause, limit_cycles),
        };

        while !self.terminated() {
            limit_cycles = self.iterate_outer(&pause, limit_cycles)?.1;
        }
        assert_eq!(self.iteration_cycles, 0);

        self.terminated_result()
    }
```
