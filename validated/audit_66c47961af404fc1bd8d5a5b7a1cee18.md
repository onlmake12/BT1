Audit Report

## Title
Fee Admission Check Uses Byte Size Only, Ignoring CKB-VM Cycle Cost — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum fee using only serialized byte size, explicitly bypassing the weight formula `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Because `verify_rtx` runs after this size-only gate and uses `max_block_cycles` as the cycle cap for local submissions with no declared cycles, an attacker can submit a ~200-byte transaction containing a looping script and force every receiving node to execute the CKB-VM for up to `max_block_cycles` cycles while paying only a ~200-shannon fee.

## Finding Description

**Root cause — size-only fee gate:**

`check_tx_fee` at `tx-pool/src/util.rs` L42–45 computes the minimum fee using only `tx_size`, with a comment explicitly acknowledging the limitation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The correct weight formula exists in `util/types/src/core/tx_pool.rs` L298–303 but is never called during admission:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [2](#0-1) 

**Exploit flow in `_process_tx`:**

1. `pre_check` is called, which invokes `check_tx_fee` with size only — passes for a ~200-byte tx paying ~200 shannons.
2. `max_cycles` is set to `declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles())` — for a local RPC call with no declared cycles, this is the full consensus `max_block_cycles`.
3. `verify_rtx` is called with this `max_cycles`, running the CKB-VM until the script completes or the cycle limit is exhausted.
4. There is **no post-verification fee check** using the actual cycle count. [3](#0-2) 

**No weight-based admission check exists anywhere in the pipeline.** The weight-based `fee_rate()` on `TxEntry` (using `get_transaction_weight`) is only used for pool ordering and eviction *after* the VM has already run. [4](#0-3) 

**Relay path is also affected:** `transactions_process.rs` only bans peers whose `declared_cycles > max_block_cycles`; declaring exactly `max_block_cycles` is permitted and triggers full-cycle verification with a size-only fee gate. [5](#0-4) 

## Impact Explanation

This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

With default mainnet config (`min_fee_rate = 1,000 shannons/KB`, `max_block_cycles ≈ 3.5B`):

| Metric | Value |
|---|---|
| Size-based min fee for ~200-byte tx | ~200 shannons |
| Actual weight at `max_block_cycles` | `3.5B × 0.000_170_571_4 ≈ 597,000 bytes` |
| Weight-correct min fee | ~597,000 shannons |
| Underpayment ratio | ~3,000× |

Each such transaction forces every node's verification worker to run the VM for up to `max_block_cycles` cycles before rejecting the transaction. With `max_ancestors_count = 25` chained descendants per UTXO, the per-UTXO impact multiplies 25×. Sustained submission saturates verification workers, delays block template assembly, and degrades node responsiveness. [6](#0-5) 

## Likelihood Explanation

- The entry path is fully unprivileged: any `send_transaction` RPC caller or P2P relay peer qualifies.
- The attacker needs valid UTXOs locked by a looping script, but the fee cost per attack transaction is negligible (~200 shannons).
- Deploying the looping script cell is a one-time on-chain cost; thereafter, any number of UTXOs can be created locked by it.
- No special knowledge or privileged access is required beyond standard CKB transaction submission.
- The relay path requires only that `declared_cycles ≤ max_block_cycles`, which is trivially satisfiable. [5](#0-4) 

## Recommendation

Replace the size-only fee check in `check_tx_fee` with a weight-based check. Since cycles are unknown at pre-check time, use `declared_cycles` (from the relay message) as an upper bound, or use `max_tx_verify_cycles` as a conservative proxy for local submissions:

```rust
let weight = get_transaction_weight(tx_size, declared_cycles.unwrap_or(max_tx_verify_cycles));
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

This ensures the fee gate is proportional to the worst-case computational cost, closing the ~3,000× underpayment gap. The `declared_cycles` value is already available in `_process_tx` and can be threaded through to `check_tx_fee`. [7](#0-6) 

## Proof of Concept

1. Deploy a CKB script cell whose bytecode loops consuming cycles until the VM halts at the cycle limit (e.g., an infinite loop in RISC-V assembly).
2. Create a UTXO locked by that script, sized to ~200 bytes serialized.
3. Submit via `send_transaction` RPC with no declared cycles. The fee check passes (~200 shannons ≥ size-based minimum of ~200 shannons).
4. Observe that `verify_rtx` runs the CKB-VM for up to `max_block_cycles` cycles before returning `ExceededMaximumCycles`.
5. Repeat with 25 chained descendants (up to `max_ancestors_count`) per UTXO to multiply impact.
6. Observe that verification workers are saturated and block template assembly latency increases.
7. For the relay path: connect as a peer, declare `cycles = max_block_cycles` in the relay message, and submit the same transaction — the peer ban check at `declared_cycles > max_block_cycles` does not trigger. [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/util.rs (L28-54)
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
}
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

**File:** tx-pool/src/process.rs (L705-732)
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
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
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
