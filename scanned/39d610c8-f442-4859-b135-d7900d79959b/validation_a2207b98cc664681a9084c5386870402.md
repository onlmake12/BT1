### Title
Unbounded `vec!` Allocation in `WeightUnitsFlow::do_estimate` via High-Fee-Rate Transaction — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

### Summary

`Algorithm::max_bucket_index_by_fee_rate` maps a `FeeRate` value to a bucket index using a linear formula with no upper bound. For fee rates achievable by submitting a valid transaction with a high fee and minimum weight, the returned index can be in the billions, causing `do_estimate` to attempt allocating hundreds of gigabytes via `vec![0u64; max_bucket_index + 1]`, crashing the node process with OOM. This affects any node that has explicitly configured `algorithm = "WeightUnitsFlow"` in `[fee_estimator]`.

---

### Finding Description

**Root cause — `max_bucket_index_by_fee_rate`:**

The last match arm is unbounded:

```rust
x => (x + t * 11_500) / (100 * t),   // t = 1000
```

For `x = 3.07 × 10¹⁷` (achievable, see below), this yields `≈ 3.07 × 10¹²`. [1](#0-0) 

**Root cause — `do_estimate` allocates twice using that index:**

```rust
let mut buckets = vec![0u64; max_bucket_index + 1];   // line 219
...
let mut buckets = vec![0u64; max_bucket_index + 1];   // line 248
```

No bounds check exists before either allocation. [2](#0-1) [3](#0-2) 

**How a high fee rate is produced — `FeeRate::calculate`:**

```rust
FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)   // KW = 1000
```

`saturating_mul` caps at `u64::MAX`. For `fee ≥ ~1.844 × 10¹⁶` shannons (~184 M CKB), `fee * 1000` saturates to `u64::MAX`, and with `weight = 60` (minimum tx size): `fee_rate = u64::MAX / 60 ≈ 3.07 × 10¹⁷`. [4](#0-3) 

**Overflow note in the last branch:** For `x > u64::MAX − 11_500_000`, the addition `x + 11_500_000` overflows u64. In release mode (wrapping arithmetic), the result wraps to a small number (0–114), so `x = u64::MAX` itself is safe. The dangerous range is `x ∈ [2_000_001, u64::MAX − 11_500_000]`, which yields indices up to `≈ 1.84 × 10¹¹`.

**Concrete allocation sizes:**

| Fee (CKB) | weight | fee_rate | max_bucket_index | Allocation (×2) |
|---|---|---|---|---|
| 10,000 | 60 | ~1.67 × 10¹³ | ~1.67 × 10⁸ | ~2.7 GB |
| 100,000 | 60 | ~1.67 × 10¹⁴ | ~1.67 × 10⁹ | ~26.8 GB |
| ≥184 M | 60 | ~3.07 × 10¹⁷ (saturated) | ~3.07 × 10¹² | ~49 TB |

**No fee-rate cap in tx-pool admission:** There is no maximum fee rate check anywhere in the tx-pool admission path. [5](#0-4) 

**RPC path:**

`estimate_fee_rate` RPC → `TxPoolController::estimate_fee_rate` → `process.rs::estimate_fee_rate` → `get_all_entry_info` → `FeeEstimator::estimate_fee_rate` → `weight_units_flow::Algorithm::estimate_fee_rate` → `do_estimate`. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

On a node with `WeightUnitsFlow` configured, a single call to `estimate_fee_rate` while a high-fee-rate transaction is in the mempool causes the tx-pool service thread to attempt a multi-GB (or multi-TB) heap allocation. The OS OOM killer terminates the process, crashing the node. The `estimate_fee_rate` RPC is unauthenticated and publicly accessible.

---

### Likelihood Explanation

`WeightUnitsFlow` is **not the default** — it requires explicit opt-in via `algorithm = "WeightUnitsFlow"` in `[fee_estimator]`. [8](#0-7) [9](#0-8) 

For nodes that do enable it: the attacker needs to own ~10,000 CKB temporarily (not permanently burned — if the node crashes before the block is confirmed, the UTXO is unspent). The attack window is the ~28-second average block interval. The economic barrier is low relative to the impact.

---

### Recommendation

Cap `max_bucket_index_by_fee_rate` to a safe constant (e.g., `MAX_BUCKET_INDEX = 1000`) before using the result to size a `Vec`:

```rust
fn max_bucket_index_by_fee_rate(fee_rate: FeeRate) -> usize {
    let index = /* existing formula */;
    (index as usize).min(MAX_BUCKET_INDEX)
}
```

Alternatively, add a guard in `do_estimate` before the `vec!` allocations:

```rust
let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate)
    .min(MAX_BUCKET_INDEX);
```

---

### Proof of Concept

```rust
// In weight_units_flow.rs tests:
#[test]
fn test_no_oom_on_high_fee_rate() {
    use ckb_types::core::{Capacity, FeeRate};
    use ckb_types::core::tx_pool::TxEntryInfo;

    // fee = 10_000 CKB = 10^12 shannons, size = 60 bytes, cycles = 0
    // weight = max(60, 0) = 60
    // fee_rate = 10^12 * 1000 / 60 ≈ 1.67e13
    // max_bucket_index ≈ 1.67e8 → vec of ~1.34 GB → OOM on most systems
    let fee_rate = FeeRate::calculate(Capacity::shannons(1_000_000_000_000u64), 60);
    let idx = Algorithm::max_bucket_index_by_fee_rate(fee_rate);
    assert!(idx < 10_000, "bucket index {} is unbounded", idx);
}
```

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** rpc/src/module/experiment.rs (L301-315)
```rust
    fn estimate_fee_rate(
        &self,
        estimate_mode: Option<EstimateMode>,
        enable_fallback: Option<bool>,
    ) -> Result<Uint64> {
        let estimate_mode = estimate_mode.unwrap_or_default();
        let enable_fallback = enable_fallback.unwrap_or(true);
        self.shared
            .tx_pool_controller()
            .estimate_fee_rate(estimate_mode.into(), enable_fallback)
            .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?
            .map_err(RPCError::from_any_error)
            .map(core::FeeRate::as_u64)
            .map(Into::into)
    }
```

**File:** shared/src/shared_builder.rs (L406-414)
```rust
        let fee_estimator_algo = fee_estimator_config
            .map(|config| config.algorithm)
            .unwrap_or(None);
        let fee_estimator = match fee_estimator_algo {
            Some(FeeEstimatorAlgo::WeightUnitsFlow) => FeeEstimator::new_weight_units_flow(),
            Some(FeeEstimatorAlgo::ConfirmationFraction) => {
                FeeEstimator::new_confirmation_fraction()
            }
            None => FeeEstimator::new_dummy(),
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
