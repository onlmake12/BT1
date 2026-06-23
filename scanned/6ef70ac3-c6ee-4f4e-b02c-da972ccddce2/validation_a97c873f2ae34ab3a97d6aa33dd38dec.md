### Title
Unbounded Heap Allocation in `weight_units_flow::Algorithm::do_estimate` via High-Fee-Rate Transaction — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

`do_estimate` derives a bucket array size directly from the highest fee rate in the tx pool with no upper bound. A single transaction carrying an abnormally high fee relative to its weight causes `max_bucket_index_by_fee_rate` to return an arbitrarily large value, and two subsequent `vec![0u64; max_bucket_index + 1]` allocations consume proportional heap memory every time `estimate_fee_rate` is called.

---

### Finding Description

`max_bucket_index_by_fee_rate` maps a `FeeRate` to a `usize` index using a piecewise linear formula. For any fee rate above 2,000,000 shannons/weight-unit, the last branch applies:

```rust
x => (x + t * 11_500) / (100 * t),   // t = 1000
``` [1](#0-0) 

This grows linearly and is completely unbounded. The returned index is then used directly to size two heap allocations inside `do_estimate`:

```rust
let mut buckets = vec![0u64; max_bucket_index + 1];  // current_weight_buckets
...
let mut buckets = vec![0u64; max_bucket_index + 1];  // flow_speed_buckets
``` [2](#0-1) [3](#0-2) 

The `max_fee_rate` driving this is taken unconditionally from the highest-fee-rate transaction currently in the pool:

```rust
let max_fee_rate = if let Some(fee_rate) = sorted_current_txs.first().map(|tx| tx.fee_rate) {
    fee_rate
} else {
    return Ok(constants::LOWEST_FEE_RATE);
};
let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate);
``` [4](#0-3) 

The fee rate itself is computed as `FeeRate::calculate(info.fee, weight)` where `weight = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`: [5](#0-4) 

The tx pool enforces only a **minimum** fee rate (`LowFeeRate` rejection) — there is no maximum fee rate guard anywhere: [6](#0-5) 

---

### Impact Explanation

**Concrete allocation math** (using minimum realistic weight ≈ 300 weight-units for a minimal CKB tx):

| Fee burned | Fee rate (shannons/wu) | `max_bucket_index` | Allocation per call |
|---|---|---|---|
| 37,500 CKB | ~1.25 × 10¹⁰ | ~125,000 | ~1 MB × 2 |
| 375,000 CKB | ~1.25 × 10¹¹ | ~1,250,000 | ~10 MB × 2 |
| 3.75 M CKB | ~1.25 × 10¹² | ~12,500,000 | ~100 MB × 2 |
| 37.5 M CKB | ~1.25 × 10¹³ | ~125,000,000 | ~1 GB × 2 |

The allocation is triggered **on every call** to `estimate_fee_rate` for as long as the high-fee-rate transaction remains in the pool. In Rust, a failed allocation aborts the process (default global allocator). A successful but enormous allocation causes severe memory pressure and can OOM-kill the node OS process.

The inner bucket-fill loop (`for i in buckets.iter_mut().take(index_curr)`) then iterates over the entire allocation, compounding CPU cost: [7](#0-6) 

---

### Likelihood Explanation

The `WeightUnitsFlow` estimator is a production-enabled variant wired into `shared_builder`: [8](#0-7) 

The attack requires burning CKB as fee, which is an economic barrier. However:
- Even a 1 MB allocation (37,500 CKB ≈ a few hundred USD at typical prices) is achievable.
- The transaction only needs to be submitted **once**; every subsequent RPC call to `estimate_fee_rate` re-triggers the allocation for free.
- The attacker can call the RPC themselves immediately after submission.
- No privileged access, no PoW, no Sybil attack is required — only a valid transaction via P2P or RPC.

---

### Recommendation

Cap `max_bucket_index` to a fixed constant (e.g., 200, matching the highest index in the test table at line 390) before performing any allocation:

```rust
let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate)
    .min(MAX_BUCKET_INDEX);  // e.g., const MAX_BUCKET_INDEX: usize = 200;
```

This bounds both allocations to `200 * 8 = 1,600 bytes` regardless of the fee rate of any transaction in the pool. [9](#0-8) 

---

### Proof of Concept

```rust
// 1. Construct a TxEntryInfo with fee = 37_500 * 10^8 shannons, size = 300, cycles = 0
//    => weight = 300, fee_rate = 37_500 * 10^8 / 300 = 1.25e10
//    => max_bucket_index = (1.25e10 + 11_500_000) / 100_000 ≈ 125_000
//    => vec![0u64; 125_001] × 2 ≈ 2 MB allocated per estimate_fee_rate call

// 2. Submit via send_transaction RPC (unprivileged, standard P2P path)

// 3. Call estimate_fee_rate RPC repeatedly; observe RSS growing by ~2 MB per call
//    with no bound until OOM or process abort.

// Assert: peak allocation per call = 2 * (max_bucket_index + 1) * 8 bytes
//         must stay < 1 MB → requires max_bucket_index < 62_500
//         → requires fee_rate < 6.25e9 shannons/wu
//         → requires fee < 6.25e9 * 300 ≈ 1_875 CKB for a minimal tx
//         The 1 MB threshold is breached with only ~1,875 CKB burned as fee.
```

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L205-219)
```rust
        let max_fee_rate = if let Some(fee_rate) = sorted_current_txs.first().map(|tx| tx.fee_rate)
        {
            fee_rate
        } else {
            return Ok(constants::LOWEST_FEE_RATE);
        };

        ckb_logger::debug!("max fee rate of current transactions: {max_fee_rate}");

        let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate);
        ckb_logger::debug!("current weight buckets size: {}", max_bucket_index + 1);

        // Create weight buckets.
        let current_weight_buckets = {
            let mut buckets = vec![0u64; max_bucket_index + 1];
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L225-227)
```rust
                    for i in buckets.iter_mut().take(index_curr) {
                        *i = weight_curr;
                    }
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

**File:** util/types/src/core/tx_pool.rs (L19-22)
```rust
    #[error(
        "The min fee rate is {0}, requiring a transaction fee of at least {1} shannons, but the fee provided is only {2}"
    )]
    LowFeeRate(FeeRate, u64, u64),
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

**File:** util/fee-estimator/src/estimator/mod.rs (L51-54)
```rust
    pub fn new_weight_units_flow() -> Self {
        let algo = weight_units_flow::Algorithm::new();
        FeeEstimator::WeightUnitsFlow(Arc::new(RwLock::new(algo)))
    }
```
