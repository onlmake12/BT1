Audit Report

## Title
Unbounded heap allocation via attacker-controlled fee rate in `do_estimate` — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

## Summary
`do_estimate` derives `max_bucket_index` directly from the highest fee rate among all current pool transactions via `max_bucket_index_by_fee_rate`, which applies no upper-bound cap on its return value. It then allocates two `Vec<u64>` of that size unconditionally. Because `check_tx_fee` enforces only a minimum fee rate floor with no maximum, an attacker can submit a valid transaction with an arbitrarily high fee rate, causing every subsequent `estimate_fee_rate` RPC call to attempt a multi-gigabyte heap allocation, resulting in process abort (OOM) or severe stall.

## Finding Description

**Unbounded `max_bucket_index_by_fee_rate`:**

The final match arm in `max_bucket_index_by_fee_rate` has no ceiling:

```rust
x => (x + t * 11_500) / (100 * t),   // t = 1000
```

For `fee_rate = 10^15`, this yields `(10^15 + 11_500_000) / 100_000 ≈ 10^10` (10 billion) with no cap applied. [1](#0-0) 

**Uncapped allocations in `do_estimate`:**

`max_bucket_index` is used directly to size two separate `Vec<u64>` allocations:

```rust
let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate);
// ...
let mut buckets = vec![0u64; max_bucket_index + 1];  // line 219
// ...
let mut buckets = vec![0u64; max_bucket_index + 1];  // line 248
```

At `max_bucket_index = 10^10`, each allocation is `10^10 × 8 = 80 GB`. Rust's global allocator calls `handle_alloc_error` on failure, which aborts the process. [2](#0-1) [3](#0-2) 

**No maximum fee rate guard in tx pool admission:**

`check_tx_fee` only enforces a minimum fee rate; there is no upper bound check:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
Ok(fee)
```

A transaction paying an enormous fee passes this check without issue. [4](#0-3) 

**Fee rate is attacker-controlled:**

`TxStatus::new_from_entry_info` computes fee rate directly from the pool entry with no saturation cap beyond `u64::MAX`: [5](#0-4) 

**Exploit path:**
1. Attacker submits a minimal valid transaction (~200 bytes) paying ~100,000 CKB as fee.
2. `fee_rate ≈ 10^13 * 1000 / 200 = 5×10^13` → `max_bucket_index ≈ 5×10^8` → 4 GB per allocation.
3. Any call to `estimate_fee_rate` RPC triggers `do_estimate` → two 4 GB allocations → OOM abort.

## Impact Explanation

**High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.**

Every call to the `estimate_fee_rate` RPC (a public, unauthenticated endpoint) triggers the two uncapped allocations sized by the highest fee rate in the pool. With a sufficiently high-fee transaction present, the allocator either aborts the process (OOM via `handle_alloc_error`) — causing a full node crash — or stalls for multiple seconds on systems with memory overcommit. The attacker's transaction persists in the pool until mined (~28 s average block interval) and can be resubmitted indefinitely to maintain the condition.

This does not rise to Critical (crashing the whole CKB network) because `WeightUnitsFlow` is non-default and must be explicitly configured, limiting the affected population to nodes that have opted into this algorithm. [6](#0-5) 

## Likelihood Explanation

- **Precondition**: `WeightUnitsFlow` must be explicitly configured — non-default but a documented, supported production option.
- **Economic cost**: The attacker must burn CKB as fee. ~100,000 CKB achieves a 4 GB allocation (sufficient for OOM on most nodes). This is a real but not prohibitive cost for a targeted attack.
- **No cryptographic or consensus barrier**: The transaction is fully valid and passes all script verification, capacity checks, and pool admission rules.
- **Repeatability**: The attacker can resubmit after each block to maintain the condition indefinitely.
- **Trigger**: Any caller of the public `estimate_fee_rate` RPC (including the attacker themselves) triggers the crash.

## Recommendation

1. **Cap `max_bucket_index`** in `max_bucket_index_by_fee_rate` to a hard constant (e.g., 10,000), matching the practical fee rate range the algorithm is designed for. The existing test data shows the algorithm is designed for fee rates up to ~3,000,000 shannons/kw (bucket index ~137).
2. **Add a guard in `do_estimate`**: clamp `max_fee_rate` before computing the bucket index, or return an error/fallback if `max_bucket_index` exceeds a safe threshold.
3. **Optionally**, add a `max_fee_rate` admission check in `check_tx_fee` to reject transactions with implausibly high fee rates.

## Proof of Concept

```rust
// Minimal reproducible unit test
#[test]
fn test_unbounded_allocation_poc() {
    // fee_rate = 10^13 (achievable with ~100k CKB fee on a 200-byte tx)
    let fee_rate = FeeRate::from_u64(10_000_000_000_000u64);
    let max_bucket_index = Algorithm::max_bucket_index_by_fee_rate(fee_rate);
    // max_bucket_index ≈ 100_000_115 → 800 MB allocation per vec
    // For fee_rate = 10^15: max_bucket_index ≈ 10^10 → 80 GB → OOM abort
    assert!(max_bucket_index > 100_000_000); // no cap exists
}
```

Manual steps:
1. Configure node with `algorithm = "WeightUnitsFlow"` in `[fee_estimator]`.
2. Submit a valid transaction with fee = 100,000 CKB and minimal size via `send_transaction` RPC.
3. Call `estimate_fee_rate` RPC — the node aborts due to OOM in `do_estimate`. [1](#0-0)

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L97-101)
```rust
    fn new_from_entry_info(info: TxEntryInfo) -> Self {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        Self { weight, fee_rate }
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L214-219)
```rust
        let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate);
        ckb_logger::debug!("current weight buckets size: {}", max_bucket_index + 1);

        // Create weight buckets.
        let current_weight_buckets = {
            let mut buckets = vec![0u64; max_bucket_index + 1];
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L245-248)
```rust
        let flow_speed_buckets = {
            let historical_tip = self.current_tip - historical_blocks;
            let sorted_flowed = self.sorted_flowed(historical_tip);
            let mut buckets = vec![0u64; max_bucket_index + 1];
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

**File:** tx-pool/src/util.rs (L44-53)
```rust
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
```

**File:** resource/ckb.toml (L306-308)
```text
# # [fee_estimator]
# # Specifies the fee estimates algorithm. Current algorithms: ConfirmationFraction, WeightUnitsFlow.
# # algorithm = "WeightUnitsFlow"
```
