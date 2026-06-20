The core math needs to be verified precisely. Let me check the `FeeRate::calculate` saturation behavior and whether there's any cap on `max_bucket_index` before the allocation.

The math and code path are fully confirmed. Here is the assessment:

---

### Title
Unbounded `vec![0u64; max_bucket_index + 1]` allocation in `do_estimate` triggered by high-fee-rate mempool transaction — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

### Summary

`do_estimate` derives `max_bucket_index` directly from the highest fee rate in the live mempool with no upper bound, then allocates two `Vec<u64>` of that size. A transaction with a sufficiently high fee rate (achievable with ~200,000 CKB and a minimal-weight transaction) causes each `estimate_fee_rate` RPC call to attempt allocating ~8 GB or more, crashing or severely degrading the node for as long as that transaction remains in the pool.

### Finding Description

`max_bucket_index_by_fee_rate` maps any fee rate above 2,000,000 shannons/KW via the unbounded formula:

```
index = (fee_rate + 11_500_000) / 100_000
``` [1](#0-0) 

For `fee_rate = 10^14`, this yields `index ≈ 1,000,000,115`. `do_estimate` then allocates **two** independent `Vec<u64>` of that length with no intervening bounds check: [2](#0-1) [3](#0-2) 

`10^9 × 8 bytes = ~8 GB` per vector, ~16 GB total per RPC call.

`TxPoolConfig` enforces only a `min_fee_rate` floor; there is no `max_fee_rate` ceiling: [4](#0-3) 

`FeeRate::calculate` uses `saturating_mul`, so the fee rate stored in a `TxEntry` is bounded only by `u64::MAX`: [5](#0-4) 

**Note on the u64::MAX edge case:** for `fee_rate` values where `fee_rate + 11_500_000` wraps around `u64::MAX` (i.e., `fee_rate > u64::MAX − 11_500_000 ≈ 1.844×10^19`), the addition wraps to a small value and the index is harmlessly small (~114). The dangerous range is therefore `fee_rate ∈ [~10^10, ~1.844×10^19 − 11_500_001]`.

### Impact Explanation

| fee_rate (shannons/KW) | `max_bucket_index` | Allocation per call |
|---|---|---|
| 10^12 | ~10^7 | ~80 MB |
| 10^13 | ~10^8 | ~800 MB |
| 10^14 | ~10^9 | ~8 GB × 2 |
| 10^15 | ~10^10 | ~80 GB × 2 (immediate OOM) |

Every `estimate_fee_rate` RPC call while the offending transaction remains in the pool will attempt these allocations. On a typical node (8–32 GB RAM), `fee_rate ≥ 10^14` causes an OOM kill or extreme memory pressure, effectively disabling fee estimation and potentially crashing the node process.

### Likelihood Explanation

**Precondition — getting the tx into the pool:** A minimal CKB transaction is ~200 bytes serialized. To achieve `fee_rate = 10^14` with `weight = 200`:

```
fee = fee_rate × weight / 1000 = 10^14 × 200 / 1000 = 2×10^13 shannons = 200,000 CKB
```

200,000 CKB is a non-trivial but realistic amount (≈0.0006% of total supply). The tx pool has no `max_fee_rate` guard, so a valid transaction paying this fee is accepted normally.

**Precondition — triggering the RPC:** `estimate_fee_rate` is unauthenticated. By default it is bound to `127.0.0.1:8114`, but many public infrastructure operators expose the RPC port. The scope explicitly includes "RPC/CLI inputs" as a valid unprivileged entry point. Even on a localhost-only node, the persistent OOM condition affects the operator's own tooling on every fee-estimation call. [6](#0-5) 

### Recommendation

1. **Cap `max_bucket_index`** before allocation. Define a compile-time constant (e.g., `MAX_BUCKET_INDEX: usize = 2000`) and clamp:
   ```rust
   let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate)
       .min(MAX_BUCKET_INDEX);
   ``` [2](#0-1) 

2. **Optionally add a `max_fee_rate` to `TxPoolConfig`** to reject economically-absurd transactions before they enter the pool.

3. **Add a test** asserting that `max_bucket_index_by_fee_rate(FeeRate::from_u64(u64::MAX))` and `max_bucket_index_by_fee_rate(FeeRate::from_u64(10u64.pow(14)))` both return values ≤ `MAX_BUCKET_INDEX`.

### Proof of Concept

```rust
// Compute allocation size for fee_rate = 10^14
let fee_rate = FeeRate::from_u64(100_000_000_000_000u64); // 10^14
let index = Algorithm::max_bucket_index_by_fee_rate(fee_rate);
// index = (100_000_000_000_000 + 11_500_000) / 100_000 = 1_000_000_115
let bytes = (index + 1) * 8;
// bytes = 8_000_000_928 ≈ 8 GB  (allocated TWICE in do_estimate)
assert!(bytes > 8_000_000_000, "allocation is ~8 GB per vec");

// Achievability: tx with size=200, fee=2×10^13 shannons (200,000 CKB)
let weight = 200u64;
let fee = Capacity::shannons(2_000_000_000_000_0u64); // 2×10^13
let computed_rate = FeeRate::calculate(fee, weight);
assert_eq!(computed_rate.as_u64(), 100_000_000_000_000u64); // 10^14 confirmed
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

**File:** util/app-config/src/configs/tx_pool.rs (L11-43)
```rust
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
    /// txs need to pay larger fee rate than this for RBF
    #[serde(with = "FeeRateDef")]
    pub min_rbf_rate: FeeRate,
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
    #[serde(default = "default_max_tx_verify_workers")]
    pub max_tx_verify_workers: usize,
    /// max ancestors size limit for a single tx
    pub max_ancestors_count: usize,
    /// rejected tx time to live by days
    pub keep_rejected_tx_hashes_days: u8,
    /// rejected tx count limit
    pub keep_rejected_tx_hashes_count: u64,
    /// The file to persist the tx pool on the disk when tx pool have been shutdown.
    ///
    /// By default, it is a subdirectory of 'tx-pool' subdirectory under the data directory.
    #[serde(default)]
    pub persisted_data: PathBuf,
    /// The recent reject record database directory path.
    ///
    /// By default, it is a subdirectory of 'tx-pool' subdirectory under the data directory.
    #[serde(default)]
    pub recent_reject: PathBuf,
    /// The expiration time for pool transactions in hours
    pub expiry_hours: u8,
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

**File:** resource/ckb.toml (L182-187)
```text
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
# }}

# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```
