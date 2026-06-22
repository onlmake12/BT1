Let me look at `FeeRate::calculate` and `get_transaction_weight` to understand the actual bounds on fee_rate values.

Now I have all the information needed. Let me work through the math precisely.

**`FeeRate::calculate`** (line 15, `util/types/src/core/fee_rate.rs`):
```
fee_rate = fee.as_u64().saturating_mul(1000) / weight
```

**`max_bucket_index_by_fee_rate`** last branch (line 357, `weight_units_flow.rs`):
```
x => (x + t * 11_500) / (100 * t)   // t = 1000
```

With `fee = 1.25e12` shannons (12,500 CKB), `weight = 100` (minimum realistic tx):
- `fee_rate = 1.25e12 * 1000 / 100 = 1.25e13`
- `max_bucket_index = (1.25e13 + 11_500_000) / 100_000 ≈ 1.25e8`
- `vec![0u64; 1.25e8 + 1]` → ~1 GB allocation

With `fee = 1.25e14` shannons (1.25M CKB):
- `max_bucket_index ≈ 1.25e10` → ~100 GB → guaranteed OOM crash

---

### Title
Unbounded `vec!` allocation in `WeightUnitsFlow::do_estimate` via attacker-controlled fee rate — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

### Summary
`do_estimate` derives `max_bucket_index` directly from the highest fee rate in the live tx pool with no upper bound, then allocates `vec![0u64; max_bucket_index + 1]` twice. A transaction with a sufficiently high fee rate causes this index to reach billions or trillions, triggering an OOM allocation that crashes any node calling `estimate_fee_rate` while `WeightUnitsFlow` is active.

### Finding Description

`FeeRate::calculate` uses `saturating_mul(1000)` before dividing by weight: [1](#0-0) 

This means for any transaction where `fee * 1000 > u64::MAX`, the fee rate saturates to `u64::MAX / weight`. With a minimum realistic tx weight of ~100, this yields `fee_rate ≈ 1.844e17`.

`max_bucket_index_by_fee_rate` has no cap — its final branch is a linear formula: [2](#0-1) 

For `fee_rate = 1.844e17`, this returns `≈ 1.844e12`. `do_estimate` then allocates two vectors of that size: [3](#0-2) [4](#0-3) 

The `max_fee_rate` is taken directly from the highest-fee-rate entry in the live pool snapshot passed to `estimate_fee_rate`, with no sanitization: [5](#0-4) 

There is no `max_fee_rate` admission check anywhere in the tx-pool — only a `min_fee_rate` floor exists.

### Impact Explanation

Any node with `WeightUnitsFlow` configured that calls `estimate_fee_rate` RPC while the attacker's tx is in the pool will attempt a multi-gigabyte (or multi-terabyte) heap allocation. On Linux this results in OOM-killer termination or a `std::alloc` abort; on other platforms it panics. The node crashes. Since the tx remains in the pool until confirmed or evicted, the crash is reproducible on every subsequent `estimate_fee_rate` call.

### Likelihood Explanation

The economic cost scales with desired impact:
- ~12,500 CKB burned → ~1 GB allocation (OOM on memory-constrained nodes)
- ~125,000 CKB burned → ~10 GB allocation (OOM on most nodes)
- ~1,250,000 CKB burned → ~100 GB allocation (OOM on all nodes)

The attack requires no special privilege — any CKB holder can submit such a transaction via P2P relay. The tx propagates to all peers, so a single submission can affect the entire network of nodes with this estimator active.

### Recommendation

Cap `max_bucket_index` to a safe constant (e.g., 10,000) in `do_estimate` before allocating:
```rust
let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate)
    .min(MAX_BUCKET_COUNT - 1);
```
Alternatively, cap the fee rate stored/used in the estimator to a reasonable maximum (e.g., 10,000,000 shannons/KW), or add a `max_fee_rate` admission guard in `accept_tx`.

### Proof of Concept

```rust
#[test]
fn test_oom_via_high_fee_rate() {
    use ckb_types::core::{Capacity, tx_pool::TxEntryInfo};
    let mut algo = Algorithm::new();
    // Simulate node being ready
    algo.update_ibd_state(false);
    // Advance tip so is_ready and historical data exist
    // (omitted for brevity — set boot_tip and current_tip appropriately)

    // Craft entry: fee = 1.25e12 shannons, size = 100 bytes, cycles = 1
    let info = TxEntryInfo {
        fee: Capacity::shannons(1_250_000_000_000u64),
        size: 100,
        cycles: 1,
        ..Default::default()
    };
    // fee_rate = 1.25e12 * 1000 / 100 = 1.25e13
    // max_bucket_index ≈ 125_000_000
    // vec![0u64; 125_000_001] = ~1 GB allocation → OOM
    let all_entry_info = /* pool with just this tx */;
    let _ = algo.estimate_fee_rate(10, all_entry_info); // panics / OOM
}
```

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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L173-184)
```rust
        let sorted_current_txs = {
            let mut current_txs: Vec<_> = all_entry_info
                .pending
                .into_values()
                .chain(all_entry_info.proposed.into_values())
                .map(TxStatus::new_from_entry_info)
                .collect();
            current_txs.sort_unstable_by(|a, b| b.cmp(a));
            current_txs
        };

        self.do_estimate(target_blocks, &sorted_current_txs)
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
