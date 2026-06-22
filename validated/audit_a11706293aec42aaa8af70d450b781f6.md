### Title
Incomplete Fee-Rate Admission Check Omits Cycles Component, Allowing Below-Minimum-Fee-Rate Transactions Into the Pool — (File: `tx-pool/src/util.rs`)

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` validates the minimum fee using only the transaction's serialized byte size, but the canonical transaction weight used everywhere else in the system is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A tx-pool submitter can craft a small-serialized-size, high-cycles transaction whose fee satisfies `fee ≥ min_fee_rate × size` yet whose true fee rate (computed with proper weight) is far below `min_fee_rate`. No second fee-rate gate is applied after cycles become known, so the transaction is admitted and persists in the pool.

### Finding Description

`check_tx_fee` computes the minimum required fee as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The code itself acknowledges the discrepancy with the comment:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly" [2](#0-1) 

Everywhere else in the system — sorting, eviction, fee estimation — the canonical weight function is used:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

`TxEntry::fee_rate()` uses this proper weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [4](#0-3) 

After `check_tx_fee` passes, `verify_rtx` executes scripts and returns the actual cycles. [5](#0-4)  The `TxEntry` is then constructed with both `size` and `cycles`. [6](#0-5)  There is no second fee-rate gate applied after cycles are known. The admission check is the only enforcement point, and it omits the cycles dimension entirely.

### Impact Explanation

An unprivileged tx-pool submitter can submit a transaction with:
- Small serialized size (e.g., 200 bytes)
- High cycles (e.g., 5,000,000 — well within `max_tx_verify_cycles`)
- Fee just above `min_fee_rate × size` (e.g., 201 shannons at `min_fee_rate = 1000 shannons/KB`)

The proper weight would be `max(200, 5_000_000 × 0.000_170_571_4) ≈ 852`. The true fee rate is `201 / 852 × 1000 ≈ 236 shannons/KB`, far below the 1000 shannons/KB threshold. Yet the transaction passes `check_tx_fee` and enters the pool. This:

1. **Bypasses the minimum fee rate policy** — the primary spam-prevention mechanism for the tx-pool.
2. **Enables pool pollution** — an attacker can fill the pool with below-threshold-fee-rate transactions, degrading the quality of the pool and affecting fee estimation.
3. **Affects fee estimation accuracy** — `FeeRateCollector` and fee estimators use proper weight, so admitted low-fee-rate transactions distort the fee market signal. [7](#0-6) 

### Likelihood Explanation

Any node with a public RPC endpoint is reachable. The `send_transaction` RPC is the standard entry point and requires no privilege. Crafting a small-size, high-cycles transaction is straightforward for any script author. The attack is cheap: the attacker pays only `min_fee_rate × size` shannons, which is far less than the fee that would be required if cycles were properly accounted for.

### Recommendation

After `verify_rtx` returns the actual cycles, apply a second fee-rate check using the proper weight:

```rust
let weight = get_transaction_weight(tx_size, completed.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors the pattern already used in `TxEntry::fee_rate()` and `estimate_fee_rate` in `pool_map.rs`. [8](#0-7) 

### Proof of Concept

1. Construct a CKB transaction with a lock script that runs a tight loop consuming ~5,000,000 cycles. Serialized size: ~200 bytes.
2. Set outputs capacity such that `fee = 201 shannons` (just above `min_fee_rate × 200 / 1000 = 200 shannons`).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`; `201 ≥ 200` → passes. [9](#0-8) 
5. `verify_rtx` executes the script, returning `cycles = 5_000_000`.
6. `TxEntry` is created; `fee_rate() = FeeRate::calculate(201, max(200, 852)) = 201/852×1000 ≈ 236 shannons/KB` — well below `min_fee_rate = 1000 shannons/KB`. [4](#0-3) 
7. The transaction is now in the pool with a fee rate 4× below the minimum threshold, with no further rejection possible.

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

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/component/entry.rs (L46-75)
```rust
impl TxEntry {
    /// Create new transaction pool entry
    pub fn new(rtx: Arc<ResolvedTransaction>, cycles: Cycle, fee: Capacity, size: usize) -> Self {
        Self::new_with_timestamp(rtx, cycles, fee, size, unix_time_as_millis())
    }

    /// Create new transaction pool entry with specified timestamp
    pub fn new_with_timestamp(
        rtx: Arc<ResolvedTransaction>,
        cycles: Cycle,
        fee: Capacity,
        size: usize,
        timestamp: u64,
    ) -> Self {
        TxEntry {
            rtx,
            cycles,
            size,
            fee,
            timestamp,
            ancestors_size: size,
            ancestors_fee: fee,
            ancestors_cycles: cycles,
            descendants_fee: fee,
            descendants_size: size,
            descendants_cycles: cycles,
            descendants_count: 1,
            ancestors_count: 1,
        }
    }
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** rpc/src/util/fee_rate.rs (L97-107)
```rust
                if let Some(cycles) = cycles {
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
                    }
```

**File:** tx-pool/src/component/pool_map.rs (L334-358)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
```
