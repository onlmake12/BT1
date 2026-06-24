Audit Report

## Title
Fee-Rate Admission Uses Size-Only Metric While Pool Stores Weight-Based Rate, Allowing Sub-Minimum-Fee-Rate Transactions Into the Pool - (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` enforces `min_fee_rate` using raw serialized byte size as the denominator before script execution, when cycles are not yet known. After verification completes and actual cycles are available, `TxEntry` is created with the weight-based fee rate (`max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`), but no second fee-rate check is performed. Any transaction where cycles dominate can pass the size-only gate with a fee that satisfies the size threshold but is far below `min_fee_rate` on a weight basis — up to ~12× below at the `max_tx_verify_cycles` limit.

## Finding Description
In `_process_tx` (`tx-pool/src/process.rs`), the flow is:

1. `pre_check` → calls `check_tx_fee` with `tx_size` (size-only gate) [1](#0-0) 
2. `verify_rtx` → actual cycles become known [2](#0-1) 
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is created with real cycles [3](#0-2) 
4. `submit_entry` is called — **no second fee-rate check using weight** [4](#0-3) 

The admission gate in `check_tx_fee` computes `min_fee = min_fee_rate.fee(tx_size as u64)`, explicitly documented as a "cheap check" using size only: [5](#0-4) 

The pool entry's actual fee rate uses `get_transaction_weight(size, cycles)` = `max(size, cycles * 0.000_170_571_4)`: [6](#0-5) 

The weight formula: [7](#0-6) 

**Concrete example:** `tx_size=1000`, `cycles=70_000_000` (max default), `min_fee_rate=1000 shannons/KW`:
- Admission requires: `1000 × 1000 / 1000 = 1000 shannons` → passes
- Actual weight: `max(1000, 70_000_000 × 0.000_170_571_4) = 11,940`
- Actual pool fee rate: `1000 × 1000 / 11940 ≈ 83 shannons/KW` — **12× below `min_fee_rate`**

The fee estimator (`weight_units_flow`) uses weight-based fee rates for all admitted transactions, so these sub-minimum-rate entries pollute its historical data: [8](#0-7) 

Eviction ordering also uses weight-based `EvictKey`, so these transactions have very low eviction priority scores and are evicted first — but until eviction they occupy pool slots: [9](#0-8) 

## Impact Explanation
An unprivileged attacker can submit transactions that bypass the effective `min_fee_rate` by up to ~12× using the default `max_tx_verify_cycles`. This enables mempool spam at a fraction of the intended cost, pollutes the `weight_units_flow` fee estimator causing it to recommend lower fee rates to users, and causes `tx_pool_info`'s reported `min_fee_rate` to be misleading. This matches the allowed impact: **"bad designs which could cause CKB network congestion with few costs"** (High, 10001–15000 points). The attacker can repeatedly fill pool capacity with high-cycle, low-effective-fee-rate transactions, displacing legitimate transactions and degrading fee estimation accuracy network-wide.

## Likelihood Explanation
Any unprivileged `send_transaction` RPC caller can trigger this. CKB-VM scripts can be arbitrarily cycle-intensive (tight loops in a lock script) while the transaction itself remains small. No special privileges, leaked keys, or victim mistakes are required. The `max_tx_verify_cycles` limit (default 70M) bounds the per-transaction cycle count but still allows a ~12× fee underestimate. This is reachable on any node with default configuration and is repeatable.

## Recommendation
After `verify_rtx` completes and actual cycles are known, perform a second fee-rate check using the weight-based metric before calling `submit_entry`:

```rust
let weight = get_transaction_weight(entry.size, entry.cycles);
let actual_fee_rate = FeeRate::calculate(entry.fee, weight);
if actual_fee_rate < tx_pool.config.min_fee_rate {
    return Some((Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, ...)), snapshot));
}
```

This should be inserted in `_process_tx` between lines 751 and 753 of `tx-pool/src/process.rs`. Alternatively, update the `TxPoolInfo.min_fee_rate` documentation and `tx_pool_info` RPC to clarify that the threshold is size-only, and adjust the fee estimator to filter out sub-threshold weight-based entries.

## Proof of Concept
1. Configure a CKB node with default `min_fee_rate = 1000` shannons/KW.
2. Deploy a lock script that runs a tight loop consuming ~70,000,000 cycles.
3. Construct a transaction spending a cell locked by that script; serialized size ≈ 1,000 bytes.
4. Set the transaction fee to exactly `1,000 shannons` (satisfies `min_fee_rate * size / 1000 = 1000`).
5. Submit via `send_transaction` RPC.
6. The transaction passes `check_tx_fee` (size-only gate) and is admitted after script verification.
7. Query `get_raw_tx_pool` (verbose=true): the transaction appears in the pool. Its `AncestorsScoreSortKey.weight` reflects the true weight (~11,940), confirming the effective fee rate is ~83 shannons/KW — 12× below `min_fee_rate`.
8. Repeat to fill the pool with such transactions, observing that `estimate_fee_rate` RPC returns progressively lower values due to fee estimator pollution.

### Citations

**File:** tx-pool/src/process.rs (L715-717)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
```

**File:** tx-pool/src/process.rs (L724-734)
```rust
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
```

**File:** tx-pool/src/process.rs (L751-751)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

**File:** tx-pool/src/process.rs (L753-753)
```rust
        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
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

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L97-101)
```rust
    fn new_from_entry_info(info: TxEntryInfo) -> Self {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        Self { weight, fee_rate }
    }
```
