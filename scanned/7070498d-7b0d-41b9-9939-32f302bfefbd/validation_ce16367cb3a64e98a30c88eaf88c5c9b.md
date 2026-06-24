Audit Report

## Title
Fee Rate Admission Check Uses `tx_size` Instead of Actual Weight, Allowing High-Cycle Transactions to Bypass `min_fee_rate` - (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using raw serialized byte count (`tx_size`) as the weight parameter, while `FeeRate` is defined as shannons per kilo-weight where weight = `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. After `verify_rtx` determines actual cycles, no second fee-rate check is performed using the real weight. This allows any unprivileged RPC caller to submit high-cycle transactions whose effective fee rate is up to ~60× below the configured `min_fee_rate`, forcing expensive script execution at negligible cost.

## Finding Description
`FeeRate` is defined as shannons per kilo-weight in `util/types/src/core/fee_rate.rs`: [1](#0-0) 

The correct weight formula uses `get_transaction_weight` from `util/types/src/core/tx_pool.rs`: [2](#0-1) 

However, `check_tx_fee` in `tx-pool/src/util.rs` passes `tx_size` directly as the weight, with a comment explicitly acknowledging the theoretical incorrectness: [3](#0-2) 

In `_process_tx` (`tx-pool/src/process.rs`), `pre_check` (which calls `check_tx_fee`) runs before `verify_rtx`. After `verify_rtx` returns actual cycles, no second fee-rate check is performed — the entry is created directly with the fee computed from `tx_size`: [4](#0-3) 

While `TxEntry.fee_rate()` and pool eviction (`EvictKey`) correctly use `get_transaction_weight` with actual cycles for ordering and eviction decisions: [5](#0-4) [6](#0-5) 

...the script execution cost is incurred **before** any eviction occurs. The node executes up to `max_block_cycles()` cycles per transaction regardless of whether the transaction is subsequently evicted.

**Concrete discrepancy:**
- `tx_size` = 200 bytes, `min_fee_rate` = 1,000 shannons/KW
- Admission check: `min_fee = 1000 × 200 / 1000 = 200 shannons`
- Actual weight at max cycles: `max(200, 70_000_000 × 0.000_170_571_4) ≈ 11,940 bytes`
- Effective fee rate: `201 × 1000 / 11940 ≈ 16.8 shannons/KW` — ~60× below the configured floor

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can submit transactions with high script cycles (up to `max_block_cycles()`) and minimal serialized size, paying fees just above `min_fee_rate × tx_size / 1000`. Each submission forces the node to execute up to 70,000,000 cycles of script verification at a cost of ~201 shannons per transaction. While pool eviction eventually removes low-fee-rate entries, the CPU cost of script execution is already paid. Sustained submission fills the verify queue with computationally expensive work, degrading node throughput and potentially causing network-wide congestion if many nodes are targeted simultaneously.

## Likelihood Explanation
- **Entry path**: Any caller of `send_transaction` RPC — no privilege required. For local RPC, `declared_cycles` is `None`, so `max_block_cycles()` is used as the cycle limit.
- **Feasibility**: Crafting a RISC-V script with a tight computation loop consuming ~70M cycles is straightforward for any script author.
- **Cost**: ~201 shannons per transaction at default parameters — negligible.
- **Persistence**: Transactions expire after `expiry_hours` (default 12 h), requiring continuous resubmission, but the cost remains trivial.
- **Existing guards**: The `limit_size` eviction and `EvictKey` ordering use actual weight, but these operate after script execution has already consumed node CPU.

## Recommendation
After `verify_rtx` returns actual cycles in `_process_tx`, perform a second fee-rate check using the real weight:

```rust
// After verify_rtx returns verified.cycles:
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_accurate = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_accurate {
    return Some((Err(Reject::LowFeeRate(
        tx_pool.config.min_fee_rate,
        min_fee_accurate.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This requires acquiring the tx_pool lock briefly after `verify_rtx` to read `min_fee_rate`, or passing the config value into `_process_tx`. Alternatively, enforce the check using declared cycles (for remote transactions) before script execution, since declared cycles are an upper bound on actual resource consumption.

## Proof of Concept
1. Compile a CKB RISC-V lock script containing a tight loop that executes ~70,000,000 cycles.
2. Construct a transaction with minimal inputs/outputs so `tx_size ≈ 200` bytes, using the above script as the lock.
3. Set fee = `min_fee_rate × tx_size / 1000 + 1` = 201 shannons (with default `min_fee_rate = 1000`).
4. Submit via `send_transaction` RPC.
5. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200`. Fee (201) ≥ min_fee (200) → **passes admission**.
6. `verify_rtx` executes the script, consuming ~70,000,000 cycles. Actual weight ≈ 11,940 bytes. Actual fee rate ≈ 16.8 shannons/KW — ~60× below the 1,000 shannons/KW floor.
7. Transaction enters the pool. Repeat in a loop to continuously force expensive script execution and fill the pool with underpriced transactions.

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-5)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);
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

**File:** tx-pool/src/process.rs (L715-751)
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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
    }
```
