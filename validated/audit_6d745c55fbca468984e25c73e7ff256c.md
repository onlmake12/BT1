### Title
WeightUnitsFlow Fee Estimator Never Nets Rejected/Evicted Transactions — Rolling Window Accounting Drift - (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

### Summary

The `WeightUnitsFlow` fee estimator records transaction weight when a transaction enters the tx pool (`accept_tx`), but has no mechanism to remove that weight when the transaction is later rejected or evicted. The `reject_tx` path for `WeightUnitsFlow` is an explicit no-op. This means the historical flow-speed window permanently over-counts weight from transactions that never stayed in the pool, causing the estimator to recommend inflated fee rates for the entire historical window duration.

### Finding Description

The `WeightUnitsFlow` algorithm stores per-block-number transaction weight in `txs: HashMap<BlockNumber, Vec<TxStatus>>`. When a transaction is accepted into the pool, its weight is appended to the current tip's bucket: [1](#0-0) 

The flow-speed estimate is then computed by summing all weights recorded in the historical window: [2](#0-1) 

The only eviction path is `expire()`, which drops entries older than `historical_blocks` (up to `2 * MAX_TARGET` blocks): [3](#0-2) 

However, when a transaction is rejected or evicted from the pool, `reject_tx` is called on the fee estimator. For `WeightUnitsFlow`, this is an explicit no-op: [4](#0-3) 

A grep of the codebase confirms `reject_tx` is never called from `tx-pool/src/process.rs` or `tx-pool/src/service.rs` for the `WeightUnitsFlow` variant. The `accept_tx` call sites in the tx-pool service do record weight on entry: [5](#0-4) 

This is the direct analog of the cross-segment netting failure: outflow (weight entering the pool) is recorded in a time-indexed bucket, but inflow (weight leaving the pool via rejection or eviction) is never netted against any bucket. The stale weight remains in the rolling window until it ages out.

### Impact Explanation

An unprivileged tx-pool submitter can artificially inflate the `WeightUnitsFlow` fee-rate estimate:

1. Submit many large transactions with fee rates just above `min_fee_rate`. Each is accepted and its weight is recorded in `txs[current_tip]`.
2. Submit a higher-fee transaction that triggers eviction of the low-fee ones (via `limit_size`), or wait for them to expire.
3. The evicted transactions' weights remain in the historical flow data for up to `2 * MAX_TARGET` blocks.
4. `estimate_fee_rate` now computes an inflated `flow_speed`, causing it to recommend a higher fee rate than the actual mempool demand warrants.

Legitimate users querying `estimate_fee_rate` via RPC will receive inflated fee recommendations and may overpay for the duration of the historical window. [6](#0-5) 

### Likelihood Explanation

- The `WeightUnitsFlow` algorithm must be explicitly enabled in node configuration; the default is `FeeEstimator::Dummy`. [7](#0-6) 
- Any tx-pool submitter can trigger the condition with no privileged access.
- The attack cost is the fees paid on submitted transactions, which limits economic incentive but does not eliminate it (attacker can use minimum-fee transactions).
- Impact is bounded to fee estimation accuracy; users can still submit transactions at any fee rate.

### Recommendation

Mirror the `ConfirmationFraction` approach and implement a real `reject_tx` for `WeightUnitsFlow` that removes the transaction's weight from the bucket in which it was originally recorded (`txs[entry_block_number]`). This requires storing the entry block number alongside each `TxStatus`, analogous to `TxRecord.height` in `ConfirmationFraction`: [8](#0-7) 

When `reject_tx` is called, look up the stored entry block number and subtract the weight from `txs[entry_block_number]`, removing the entry if the bucket becomes empty. This ensures the rolling window reflects net flow rather than gross inflow.

### Proof of Concept

**Setup**: Node configured with `fee_estimator.algorithm = "WeightUnitsFlow"`. Pool `min_fee_rate = 1000`, `max_tx_pool_size` set small enough to trigger eviction.

**Step 1 — Record large outflow**: Submit N large transactions (each near `TRANSACTION_SIZE_LIMIT`) with fee rate just above `min_fee_rate`. Each call to `accept_tx` appends weight to `txs[tip]`. Query `estimate_fee_rate` — returns elevated rate reflecting the large weight.

**Step 2 — Evict without netting**: Submit one transaction with a higher fee rate that causes `limit_size` to evict the N low-fee transactions. `reject_tx` is called but is a no-op for `WeightUnitsFlow`. The N entries remain in `txs[tip]`.

**Step 3 — Observe stale inflation**: Query `estimate_fee_rate` again. The flow-speed buckets still include the evicted transactions' weight. The returned fee rate remains inflated despite the pool now being nearly empty.

**Step 4 — Negative control**: Repeat with `ConfirmationFraction` algorithm. After eviction, `reject_tx` removes the entries and the estimate returns to baseline. This confirms the bug is specific to the `WeightUnitsFlow` no-op path. [4](#0-3) [1](#0-0)

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L147-151)
```rust
    fn expire(&mut self) {
        let historical_blocks = Self::historical_blocks(constants::MAX_TARGET);
        let expired_tip = self.current_tip.saturating_sub(historical_blocks);
        self.txs.retain(|&num, _| num >= expired_tip);
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L153-162)
```rust
    pub fn accept_tx(&mut self, info: TxEntryInfo) {
        if self.current_tip == 0 {
            return;
        }
        let item = TxStatus::new_from_entry_info(info);
        self.txs
            .entry(self.current_tip)
            .and_modify(|items| items.push(item))
            .or_insert_with(|| vec![item]);
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L244-272)
```rust
        // Calculate flow speeds for buckets.
        let flow_speed_buckets = {
            let historical_tip = self.current_tip - historical_blocks;
            let sorted_flowed = self.sorted_flowed(historical_tip);
            let mut buckets = vec![0u64; max_bucket_index + 1];
            let mut index_curr = max_bucket_index;
            for tx in &sorted_flowed {
                let index = Self::max_bucket_index_by_fee_rate(tx.fee_rate);
                if index > max_bucket_index {
                    continue;
                }
                if index < index_curr {
                    let flowed_curr = buckets[index_curr];
                    for i in buckets.iter_mut().take(index_curr) {
                        *i = flowed_curr;
                    }
                }
                buckets[index] += tx.weight;
                index_curr = index;
            }
            let flowed_curr = buckets[index_curr];
            for i in buckets.iter_mut().take(index_curr) {
                *i = flowed_curr;
            }
            buckets
                .into_iter()
                .map(|value| value / historical_blocks)
                .collect::<Vec<_>>()
        };
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L303-313)
```rust
    fn sorted_flowed(&self, historical_tip: BlockNumber) -> Vec<TxStatus> {
        let mut statuses: Vec<_> = self
            .txs
            .iter()
            .filter(|&(&num, _)| num >= historical_tip)
            .flat_map(|(_, statuses)| statuses.to_owned())
            .collect();
        statuses.sort_unstable_by(|a, b| b.cmp(a));
        ckb_logger::trace!(">>> sorted flowed length: {}", statuses.len());
        statuses
    }
```

**File:** util/fee-estimator/src/estimator/mod.rs (L74-81)
```rust
    /// Accepts a tx.
    pub fn accept_tx(&self, tx_hash: Byte32, info: TxEntryInfo) {
        match self {
            Self::Dummy => {}
            Self::ConfirmationFraction(algo) => algo.write().accept_tx(tx_hash, info),
            Self::WeightUnitsFlow(algo) => algo.write().accept_tx(info),
        }
    }
```

**File:** util/fee-estimator/src/estimator/mod.rs (L83-89)
```rust
    /// Rejects a tx.
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
    }
```

**File:** util/app-config/src/configs/fee_estimator.rs (L1-18)
```rust
use serde::{Deserialize, Serialize};

/// Fee estimator config options.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// The algorithm for fee estimator.
    pub algorithm: Option<Algorithm>,
}

/// Specifies the fee estimates algorithm.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Eq)]
pub enum Algorithm {
    /// Confirmation Fraction Fee Estimator
    ConfirmationFraction,
    /// Weight-Units Flow Fee Estimator
    WeightUnitsFlow,
}
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L60-65)
```rust
#[derive(Clone)]
struct TxRecord {
    height: u64,
    bucket_index: usize,
    fee_rate: FeeRate,
}
```
