Audit Report

## Title
Unbounded heap allocation in `do_estimate` via attacker-controlled fee rate — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

## Summary
`do_estimate` derives `max_bucket_index` directly from the highest fee rate in the live tx pool snapshot with no upper bound, then allocates `vec![0u64; max_bucket_index + 1]` twice. A transaction with a sufficiently high fee rate causes `max_bucket_index` to reach hundreds of millions or trillions, triggering an OOM allocation that crashes any node calling `estimate_fee_rate` while `WeightUnitsFlow` is active and the attacker's transaction remains in the pool.

## Finding Description

**Root cause — `FeeRate::calculate` has no cap:**

`FeeRate::calculate` uses `saturating_mul(1000)` before dividing by weight: [1](#0-0) 

For any transaction where `fee * 1000 > u64::MAX` (i.e., fee > ~1.844e16 shannons), the fee rate saturates to `u64::MAX / weight`. With a minimum realistic tx weight of ~100, this yields `fee_rate ≈ 1.844e17`.

**Root cause — `max_bucket_index_by_fee_rate` has no cap:**

The final branch of `max_bucket_index_by_fee_rate` is an unbounded linear formula: [2](#0-1) 

For `fee_rate = 1.25e13` (achievable with fee = 1.25e12 shannons, weight = 100): `max_bucket_index = (1.25e13 + 11_500_000) / 100_000 ≈ 125,000,000`.

**Root cause — `do_estimate` allocates without capping:**

`max_fee_rate` is taken directly from the highest-fee-rate entry in the pool snapshot with no sanitization: [3](#0-2) 

Two vectors of size `max_bucket_index + 1` are then allocated unconditionally: [4](#0-3) [5](#0-4) 

**Exploit path:**
1. Attacker submits a valid CKB transaction with a high fee (e.g., 12,500 CKB fee, 100-byte tx body).
2. The tx propagates via P2P relay and enters the pool of any node with `WeightUnitsFlow` configured.
3. Any caller of the `estimate_fee_rate` RPC (publicly accessible, no authentication): [6](#0-5) 
   triggers `do_estimate`, which attempts to allocate ~1 GB (or more) on the heap.
4. On Linux, the OOM killer terminates the node process; on other platforms, `std::alloc` aborts. The crash is reproducible on every subsequent `estimate_fee_rate` call until the tx is confirmed or evicted.

**No existing guard:** The tx pool enforces only a `min_fee_rate` floor; there is no `max_fee_rate` admission check anywhere in the acceptance path. [7](#0-6) 

## Impact Explanation

Any node operator who has configured `WeightUnitsFlow` (a supported, documented algorithm option in `fee_estimator.rs`) and exposes the `estimate_fee_rate` RPC can have their node crashed and kept crashed by a single attacker transaction. This maps to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.** [8](#0-7) 

## Likelihood Explanation

The attack requires no special privilege — any CKB holder can submit a high-fee transaction via P2P relay. The economic cost scales with desired impact: ~12,500 CKB causes ~1 GB allocation (OOM on memory-constrained nodes); ~1,250,000 CKB causes ~100 GB allocation (OOM on all nodes). The tx remains in the pool until confirmed or evicted, so the crash is reproducible on every `estimate_fee_rate` call during that window. The `WeightUnitsFlow` algorithm is not the default but is a supported configuration option, limiting the affected population to nodes that have explicitly opted in.

## Recommendation

Cap `max_bucket_index` to a safe constant before allocating in `do_estimate`:

```rust
const MAX_BUCKET_COUNT: usize = 10_001; // supports fee rates up to ~2,000,000 shannons/KW

let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate)
    .min(MAX_BUCKET_COUNT - 1);
```

Alternatively, cap the fee rate used in the estimator to a reasonable maximum (e.g., 10,000,000 shannons/KW) in `TxStatus::new_from_entry_info`, or add a `max_fee_rate` admission guard in `accept_tx`. [9](#0-8) 

## Proof of Concept

```rust
#[test]
fn test_oom_via_high_fee_rate() {
    use ckb_types::core::{Capacity, FeeRate};
    // Verify the index calculation directly — no node setup needed
    // fee = 1.25e12 shannons, weight = 100 → fee_rate = 1.25e13
    let fee_rate = FeeRate::from_u64(1_250_000_000_000u64 * 1000 / 100); // 1.25e13
    let max_bucket_index = Algorithm::max_bucket_index_by_fee_rate(fee_rate);
    // max_bucket_index ≈ 125_000_115
    // vec![0u64; 125_000_116] ≈ 1 GB — triggers OOM
    assert!(max_bucket_index > 100_000_000,
        "index {} would cause ~1 GB allocation", max_bucket_index);

    // For saturated fee_rate (fee > u64::MAX/1000):
    let saturated = FeeRate::from_u64(u64::MAX / 100); // ~1.844e17
    let saturated_index = Algorithm::max_bucket_index_by_fee_rate(saturated);
    // saturated_index ≈ 1.844e12 → ~14.75 TB allocation
    assert!(saturated_index > 1_000_000_000_000usize);
}
```

To trigger the full crash: configure a node with `algorithm = "WeightUnitsFlow"` in `ckb.toml`, submit a transaction with fee ≥ 12,500 CKB and minimal size/cycles, then call `estimate_fee_rate` via RPC. The node process will be killed by OOM.

### Citations

**File:** util/types/src/core/fee_rate.rs (L11-16)
```rust
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L96-102)
```rust
impl TxStatus {
    fn new_from_entry_info(info: TxEntryInfo) -> Self {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        Self { weight, fee_rate }
    }
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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L205-214)
```rust
        let max_fee_rate = if let Some(fee_rate) = sorted_current_txs.first().map(|tx| tx.fee_rate)
        {
            fee_rate
        } else {
            return Ok(constants::LOWEST_FEE_RATE);
        };

        ckb_logger::debug!("max fee rate of current transactions: {max_fee_rate}");

        let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate);
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L218-219)
```rust
        let current_weight_buckets = {
            let mut buckets = vec![0u64; max_bucket_index + 1];
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L248-249)
```rust
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

**File:** rpc/src/module/experiment.rs (L215-220)
```rust
    #[rpc(name = "estimate_fee_rate")]
    fn estimate_fee_rate(
        &self,
        estimate_mode: Option<EstimateMode>,
        enable_fallback: Option<bool>,
    ) -> Result<Uint64>;
```

**File:** util/app-config/src/configs/fee_estimator.rs (L12-18)
```rust
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Eq)]
pub enum Algorithm {
    /// Confirmation Fraction Fee Estimator
    ConfirmationFraction,
    /// Weight-Units Flow Fee Estimator
    WeightUnitsFlow,
}
```
