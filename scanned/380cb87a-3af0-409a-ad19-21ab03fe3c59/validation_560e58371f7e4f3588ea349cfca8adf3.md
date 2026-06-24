Audit Report

## Title
Unbounded heap allocation in `WeightUnitsFlow::do_estimate` via attacker-controlled fee rate — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

## Summary
`do_estimate` derives `max_bucket_index` directly from the highest fee rate in the live tx pool snapshot with no upper bound, then allocates `vec![0u64; max_bucket_index + 1]` twice. An attacker submitting a transaction with a sufficiently high fee-to-weight ratio causes `max_bucket_index` to reach hundreds of millions or higher, triggering a multi-gigabyte heap allocation that crashes any node with `WeightUnitsFlow` configured that calls `estimate_fee_rate`.

## Finding Description

`FeeRate::calculate` multiplies fee by 1000 before dividing by weight using `saturating_mul`: [1](#0-0) 

For a transaction with `fee = 1.25e12` shannons and `weight = 100` (minimum realistic), this yields `fee_rate = 1.25e13`.

`max_bucket_index_by_fee_rate` has no cap — its final branch is an unbounded linear formula: [2](#0-1) 

For `fee_rate = 1.25e13`, this returns `(1.25e13 + 11_500_000) / 100_000 ≈ 125_000_115`.

`do_estimate` then allocates two vectors of that size with no cap on `max_bucket_index`: [3](#0-2) [4](#0-3) 

`max_fee_rate` is taken directly from the highest-fee-rate entry in the live pool snapshot with no sanitization: [5](#0-4) 

`accept_tx` stores incoming transactions with no fee rate admission check: [6](#0-5) 

Exploit path:
1. Attacker submits a tx with `fee = 1.25e12` shannons and minimal weight (~100)
2. Tx propagates via P2P relay to all peers
3. Any node with `WeightUnitsFlow` configured calls `estimate_fee_rate` RPC
4. `do_estimate` computes `max_bucket_index ≈ 125_000_115`
5. `vec![0u64; 125_000_116]` allocates ~1 GB twice → OOM crash

With saturated fee rate (`u64::MAX / 100 ≈ 1.844e17`), `max_bucket_index ≈ 1.844e12`, yielding ~14.7 TB allocation — guaranteed OOM on any hardware.

## Impact Explanation

**High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.**

Any node with `WeightUnitsFlow` configured (via `fee_estimator.algorithm = WeightUnitsFlow` in the node config) that calls `estimate_fee_rate` while the attacker's transaction is in the pool will attempt a multi-gigabyte heap allocation. On Linux this results in OOM-killer termination or a `std::alloc` abort; on other platforms it panics. The crash is reproducible on every subsequent `estimate_fee_rate` call until the tx is confirmed or evicted.

## Likelihood Explanation

Any CKB holder can submit such a transaction via P2P relay — no special privilege required. The transaction propagates to all peers, so a single submission affects all nodes with `WeightUnitsFlow` active simultaneously. The economic cost is proportional to the desired impact (~12,500 CKB for ~1 GB allocation). The attack is repeatable: the attacker can submit new high-fee transactions continuously to maintain the crash condition.

## Recommendation

Cap `max_bucket_index` to a safe constant before allocating in `do_estimate`:

```rust
const MAX_BUCKET_COUNT: usize = 10_000;

let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate)
    .min(MAX_BUCKET_COUNT - 1);
```

Alternatively, cap the fee rate at ingestion time in `accept_tx` and in `estimate_fee_rate`'s pool snapshot processing to a reasonable maximum (e.g., 10,000,000 shannons/KW), so that `max_bucket_index` is bounded before any allocation occurs.

## Proof of Concept

```rust
#[test]
fn test_oom_via_high_fee_rate() {
    use ckb_types::core::FeeRate;
    // fee_rate = 1.25e13 → max_bucket_index ≈ 125_000_115
    // vec![0u64; 125_000_116] ≈ 1 GB × 2 allocations → OOM
    let bucket_index =
        Algorithm::max_bucket_index_by_fee_rate(FeeRate::from_u64(12_500_000_000_000));
    assert!(bucket_index > 100_000_000); // ~125 million
    // Allocating vec![0u64; bucket_index + 1] would consume ~1 GB
    // In do_estimate this happens unconditionally before any bounds check
}
```

To trigger the full crash: configure a node with `WeightUnitsFlow`, submit a transaction with `fee = 1_250_000_000_000` shannons and `size = 100` bytes, then call the `estimate_fee_rate` RPC. The node process will be killed by the OOM killer or panic on allocation failure.

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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L205-210)
```rust
        let max_fee_rate = if let Some(fee_rate) = sorted_current_txs.first().map(|tx| tx.fee_rate)
        {
            fee_rate
        } else {
            return Ok(constants::LOWEST_FEE_RATE);
        };
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L214-219)
```rust
        let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate);
        ckb_logger::debug!("current weight buckets size: {}", max_bucket_index + 1);

        // Create weight buckets.
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
