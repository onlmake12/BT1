Audit Report

## Title
Size-Only Fee Pre-Check Allows Forced Maximum-Cycle Script Execution at Minimum Cost — (`tx-pool/src/util.rs`, `tx-pool/src/process.rs`)

## Summary

`check_tx_fee` gates tx-pool admission using only the serialized byte size of a transaction, while actual computational cost is measured in cycles. An attacker who deploys a script consuming exactly `C` cycles (up to `max_block_cycles`) can relay a small transaction, pass the size-based fee gate with a trivially small fee, and force every receiving node to execute the script for `C` cycles during tx-pool admission. The fee paid is proportional to byte size; the CPU cost imposed is proportional to cycles — a resource-accounting gap of up to ~597× at default parameters.

## Finding Description

**Root cause — fee pre-check ignores cycles:**

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only `tx_size`:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The comment at lines 42–44 explicitly acknowledges this is a deliberate approximation. [1](#0-0) 

The correct weight formula exists in `util/types/src/core/tx_pool.rs` as `get_transaction_weight`, which takes `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`, but it is never called from `check_tx_fee`. [2](#0-1) 

**Root cause — declared_cycles used as execution limit without weight-based fee check:**

In `_process_tx`, `pre_check` (which calls `check_tx_fee`) runs first, then `verify_rtx` is called with `max_cycles = declared_cycles`:

```rust
let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
// ...
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
// ...
let verified_ret = verify_rtx(..., max_cycles, command_rx).await;
``` [3](#0-2) 

The fee check at `pre_check` line 289 uses only `tx_size`, not `declared_cycles`. [4](#0-3) 

**Insufficient guard at relay admission:**

The only upstream guard on `declared_cycles` in `sync/src/relayer/transactions_process.rs` is that it must not exceed `max_block_cycles`. There is no check against `max_tx_verify_cycles` at this stage. [5](#0-4) 

**`max_tx_verify_cycles` does not cap execution:**

The `VerifyQueue` uses `max_tx_verify_cycles` only as a scheduling threshold (`large_cycle_threshold`) to route large-cycle txs to a different worker. It does not reject or cap the execution of such transactions. [6](#0-5) 

The `_process_tx` function, called by `VerifyMgr` workers, still passes `declared_cycles` directly to `verify_rtx` regardless of `max_tx_verify_cycles`. [7](#0-6) 

**`DeclaredWrongCycles` does not prevent the attack:**

After script execution, if `declared != verified.cycles`, the tx is rejected. This requires the attacker to craft a script that consumes *exactly* `C` cycles — feasible with a deterministic RISC-V loop. [8](#0-7) 

**Execution path:**

`TransactionsProcess::execute` → `submit_remote_tx` → `enqueue_verify_queue` → `VerifyMgr` worker → `_process_tx` → `pre_check` / `check_tx_fee(tx_size)` [passes] → `verify_rtx(max_cycles = declared_cycles)` [scripts run for C cycles] → pool admission.

## Impact Explanation

This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points).**

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` and `max_block_cycles ≈ 3.5 × 10⁹`, the weight of a maximum-cycle transaction is ~597,000 bytes, requiring ~597,000 shannons at the default 1,000 shannons/KB rate. A 200-byte transaction passes the pre-check with ~200 shannons — a ~2,985× underpayment for the actual computational cost. [9](#0-8) 

Repeated submissions saturate the `VerifyMgr` worker pool, degrading transaction throughput for all legitimate users and potentially causing the verify queue to back up. The `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256MB` limit is byte-based and does not bound total cycle work. [10](#0-9) 

## Likelihood Explanation

Any unprivileged actor who can deploy a script cell and relay a transaction can trigger this path. No privileged keys, majority hashpower, or Sybil capability is required. The attacker only needs to pay the minimum size-based fee. The attack is repeatable across multiple cells or via RBF, and scales with the number of connected peers willing to relay the transaction.

## Recommendation

1. **Pre-check fee against declared cycles before script execution**: In `check_tx_fee`, compute `weight = max(tx_size, declared_cycles * DEFAULT_BYTES_PER_CYCLES)` and require `fee ≥ min_fee_rate.fee(weight)`. Pass `declared_cycles` as an additional parameter to `check_tx_fee`.
2. **Enforce `max_tx_verify_cycles` at relay admission**: In `TransactionsProcess::execute`, reject (and ban) peers whose `declared_cycles` exceeds `max_tx_verify_cycles`, not just `max_block_cycles`. This caps per-transaction CPU exposure at the pool's configured limit.
3. **Rate-limit verify queue entries per peer**: Bound the number of queued verifications attributable to a single peer to prevent a single source from saturating the worker pool.

## Proof of Concept

1. Deploy a RISC-V lock script that executes a deterministic loop consuming exactly `C` cycles (e.g., `C = max_block_cycles`) and returns exit code 0.
2. Create a live cell on-chain locked by this script.
3. Construct a transaction spending that cell. Serialized size: ~200 bytes. Required fee at 1,000 shannons/KB: ~200 shannons.
4. Relay the transaction to a target node via `RelayTransactions` with `declared_cycles = C`.
5. The node's `check_tx_fee` passes (200 shannons ≥ 200 shannons minimum for 200 bytes). [11](#0-10) 
6. `verify_rtx` is called with `max_cycles = C`; the VM executes for `C` cycles. [12](#0-11) 
7. The script exits with code 0; `verified.cycles == declared_cycles`; the transaction is admitted to the pool.
8. Repeat from step 3 with a new input (or use RBF) to continuously force `C`-cycle executions on the node.

### Citations

**File:** tx-pool/src/util.rs (L42-52)
```rust
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
```

**File:** tx-pool/src/util.rs (L85-131)
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
```

**File:** util/types/src/core/tx_pool.rs (L276-279)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;
```

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/process.rs (L286-294)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
```

**File:** tx-pool/src/process.rs (L715-732)
```rust
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
```

**File:** tx-pool/src/process.rs (L736-748)
```rust
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

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L212-214)
```rust
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
```

**File:** tx-pool/src/verify_mgr.rs (L147-154)
```rust
            if let Some((res, snapshot)) = self
                .service
                ._process_tx(
                    entry.tx.clone(),
                    entry.remote.map(|e| e.0),
                    Some(&mut self.command_rx),
                )
                .await
```
