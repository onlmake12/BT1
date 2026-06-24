Audit Report

## Title
Unbounded `vec!` Allocation in `do_estimate` via Uncapped `max_bucket_index_by_fee_rate` — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

## Summary

`Algorithm::max_bucket_index_by_fee_rate` applies a linear formula with no upper bound on its final match arm, allowing arbitrarily large bucket indices for high fee rates. `do_estimate` uses this index directly to size two `Vec` allocations without any cap or guard. A single high-fee-rate transaction in the mempool, combined with a call to the `estimate_fee_rate` RPC, causes the tx-pool service thread to attempt a multi-GB heap allocation, crashing the node via OOM.

## Finding Description

**Root cause 1 — unbounded `max_bucket_index_by_fee_rate`:**

The final match arm in `max_bucket_index_by_fee_rate` is:
```rust
x => (x + t * 11_500) / (100 * t),   // t = 1000
``` [1](#0-0) 

There is no cap on the returned `usize`. For `x ≈ 1.67 × 10¹³` (fee rate from a 10,000 CKB fee on a 60-byte tx), the index is `≈ 1.67 × 10⁸`.

**Root cause 2 — unchecked `vec!` allocations in `do_estimate`:**

```rust
let mut buckets = vec![0u64; max_bucket_index + 1];  // line 219
...
let mut buckets = vec![0u64; max_bucket_index + 1];  // line 248
``` [2](#0-1) [3](#0-2) 

No bounds check exists before either allocation.

**Root cause 3 — `FeeRate::calculate` uses `saturating_mul`:**

```rust
FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
``` [4](#0-3) 

For `fee ≥ ~1.844 × 10¹⁶` shannons, `fee * 1000` saturates to `u64::MAX`. With `weight = 60`, `fee_rate ≈ 3.07 × 10¹⁷`, yielding a bucket index in the trillions.

**Exploit path:**

1. Attacker submits a transaction with a high fee (e.g., 10,000 CKB) and minimum weight (60 bytes) to the mempool — no permanent fund loss required if the node crashes before the block is confirmed.
2. Attacker (or any caller) invokes the `estimate_fee_rate` RPC.
3. `process.rs::estimate_fee_rate` calls `get_all_entry_info` then `FeeEstimator::estimate_fee_rate` → `Algorithm::estimate_fee_rate` → `do_estimate`. [5](#0-4) 
4. `do_estimate` computes `max_bucket_index ≈ 1.67 × 10⁸` (for 10,000 CKB fee) and attempts two `vec![0u64; ...]` allocations totaling ~2.7 GB. The OS OOM killer terminates the process.

**No existing guards:** There is no fee-rate cap in tx-pool admission, no maximum bucket index constant, and no pre-allocation size check anywhere in this path.

## Impact Explanation

This matches **High: Vulnerabilities which could easily crash a CKB node**. A node operator who has opted into `WeightUnitsFlow` (via `algorithm = "WeightUnitsFlow"` in `[fee_estimator]`) can have their node crashed remotely by any party who can submit a transaction and call the unauthenticated `estimate_fee_rate` RPC. The crash is deterministic and repeatable.

## Likelihood Explanation

`WeightUnitsFlow` is not the default and requires explicit opt-in, which limits the affected population. However, for nodes that do enable it, the attack requires only a temporary capital outlay (~10,000 CKB) and a single RPC call. The attacker does not permanently lose funds if the node crashes before block confirmation. The attack is repeatable and requires no special privileges or insider access.

## Recommendation

Cap the return value of `max_bucket_index_by_fee_rate` to a safe constant before use:

```rust
const MAX_BUCKET_INDEX: usize = 1000;

fn max_bucket_index_by_fee_rate(fee_rate: FeeRate) -> usize {
    let t = FEE_RATE_UNIT;
    let index = match fee_rate.as_u64() {
        // ... existing arms ...
        x => (x + t * 11_500) / (100 * t),
    };
    (index as usize).min(MAX_BUCKET_INDEX)
}
```

Alternatively, add a guard in `do_estimate` immediately after computing `max_bucket_index`:

```rust
let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate)
    .min(MAX_BUCKET_INDEX);
```

## Proof of Concept

```rust
#[test]
fn test_no_oom_on_high_fee_rate() {
    use ckb_types::core::{Capacity, FeeRate};
    // fee = 10_000 CKB = 10^12 shannons, weight = 60
    // fee_rate = 10^12 * 1000 / 60 ≈ 1.67e13
    // max_bucket_index ≈ 1.67e8 → two vec allocs of ~1.34 GB each → OOM
    let fee_rate = FeeRate::calculate(Capacity::shannons(1_000_000_000_000u64), 60);
    let idx = Algorithm::max_bucket_index_by_fee_rate(fee_rate);
    assert!(idx < 10_000, "bucket index {} is unbounded and will cause OOM", idx);
}
```

This test will fail on the current code (index ≈ 167,000,000), demonstrating the unbounded growth. Running `do_estimate` with a mempool entry at this fee rate will trigger an OOM crash on any system without sufficient free memory.

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L214-219)
```rust
        let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate);
        ckb_logger::debug!("current weight buckets size: {}", max_bucket_index + 1);

        // Create weight buckets.
        let current_weight_buckets = {
            let mut buckets = vec![0u64; max_bucket_index + 1];
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L244-249)
```rust
        // Calculate flow speeds for buckets.
        let flow_speed_buckets = {
            let historical_tip = self.current_tip - historical_blocks;
            let sorted_flowed = self.sorted_flowed(historical_tip);
            let mut buckets = vec![0u64; max_bucket_index + 1];
            let mut index_curr = max_bucket_index;
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L348-360)
```rust
    fn max_bucket_index_by_fee_rate(fee_rate: FeeRate) -> usize {
        let t = FEE_RATE_UNIT;
        let index = match fee_rate.as_u64() {
            x if x <= 10_000 => x / t,
            x if x <= 50_000 => (x + t * 10) / (2 * t),
            x if x <= 200_000 => (x + t * 100) / (5 * t),
            x if x <= 500_000 => (x + t * 400) / (10 * t),
            x if x <= 1_000_000 => (x + t * 1_300) / (20 * t),
            x if x <= 2_000_000 => (x + t * 4_750) / (50 * t),
            x => (x + t * 11_500) / (100 * t),
        };
        index as usize
    }
```

**File:** util/types/src/core/fee_rate.rs (L11-16)
```rust
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
    }
```

**File:** tx-pool/src/process.rs (L945-970)
```rust
    pub(crate) async fn estimate_fee_rate(
        &self,
        estimate_mode: EstimateMode,
        enable_fallback: bool,
    ) -> Result<FeeRate, AnyError> {
        let all_entry_info = self.tx_pool.read().await.get_all_entry_info();
        match self
            .fee_estimator
            .estimate_fee_rate(estimate_mode, all_entry_info)
        {
            Ok(fee_rate) => Ok(fee_rate),
            Err(err) => {
                if enable_fallback {
                    let target_blocks =
                        FeeEstimator::target_blocks_for_estimate_mode(estimate_mode);
                    self.tx_pool
                        .read()
                        .await
                        .estimate_fee_rate(target_blocks)
                        .map_err(Into::into)
                } else {
                    Err(err.into())
                }
            }
        }
    }
```
