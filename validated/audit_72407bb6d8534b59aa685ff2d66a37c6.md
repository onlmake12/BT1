### Title
Unbounded `vec![0u64; max_bucket_index + 1]` allocation in `do_estimate` via attacker-controlled fee rate — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

When the `WeightUnitsFlow` fee estimator is active, `do_estimate` derives `max_bucket_index` directly from the highest fee rate among all current pool transactions, then allocates two `Vec<u64>` of that size with no upper-bound cap. Because the tx pool imposes no maximum fee rate, an attacker who submits a single small transaction with an astronomically high fee rate (achieved by burning their own CKB as fee) can force every subsequent `estimate_fee_rate` RPC call to attempt a multi-gigabyte or multi-terabyte heap allocation, causing OOM abort or a multi-second stall.

---

### Finding Description

**Root cause — unbounded bucket index:**

`max_bucket_index_by_fee_rate` maps any `u64` fee rate to a `usize` index with no ceiling:

```rust
// util/fee-estimator/src/estimator/weight_units_flow.rs, line 357
x => (x + t * 11_500) / (100 * t),   // t = 1000
```

For `fee_rate = 10^15` shannons/kw this yields `(10^15 + 11_500_000) / 100_000 ≈ 10^10` (10 billion). [1](#0-0) 

**Root cause — uncapped allocation:**

`do_estimate` allocates two separate vectors of that size unconditionally:

```rust
let mut buckets = vec![0u64; max_bucket_index + 1];   // line 219
// ...
let mut buckets = vec![0u64; max_bucket_index + 1];   // line 248
```

At `max_bucket_index = 10^10`, each allocation is `10^10 × 8 = 80 GB`. Rust's global allocator calls `handle_alloc_error` on failure, which **aborts the process**. [2](#0-1) 

**Root cause — no max fee rate guard in tx pool:**

`check_tx_fee` only enforces a *minimum* fee rate floor; there is no `max_fee_rate` check anywhere in the admission path:

```rust
// tx-pool/src/util.rs
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
Ok(fee)
```

A transaction paying an enormous fee passes this check without issue. [3](#0-2) 

**Fee rate is attacker-controlled:**

`TxStatus::new_from_entry_info` computes fee rate from the actual fee and weight stored in the pool entry, with no saturation cap beyond `u64::MAX`:

```rust
let fee_rate = FeeRate::calculate(info.fee, weight);
```

`FeeRate::calculate` uses `saturating_mul(KW) / weight`, so for a minimal transaction (weight ≈ 200 bytes) paying 100,000 CKB as fee: `fee_rate ≈ 10^13 * 1000 / 200 = 5 × 10^13`, yielding `max_bucket_index ≈ 5 × 10^8` → 4 GB allocation. [4](#0-3) 

**`WeightUnitsFlow` is opt-in but a documented production option:**

The config template explicitly documents it as a selectable production algorithm:

```toml
# [fee_estimator]
# algorithm = "WeightUnitsFlow"
``` [5](#0-4) 

---

### Impact Explanation

Every call to the `estimate_fee_rate` RPC (a public, unauthenticated endpoint) triggers `do_estimate` → two `vec![0u64; max_bucket_index + 1]` allocations sized by the highest fee rate in the pool. With a sufficiently high-fee transaction present, the allocator either:
- **Aborts the process** (OOM via `handle_alloc_error`) — node crash, full DoS until restart and pool drain.
- **Stalls for seconds** on systems with overcommit — multi-second freeze on every RPC call.

The attacker's transaction remains in the pool until mined, so the window persists for at least one block interval (~28 s average). The attacker can re-submit to maintain the condition indefinitely.

---

### Likelihood Explanation

- **Precondition**: `WeightUnitsFlow` must be explicitly configured. This is non-default but is a documented, supported production option.
- **Economic cost**: The attacker must burn CKB as fee. For a 4 GB allocation (sufficient for OOM on most nodes), ~100,000 CKB is required. This is a real but not prohibitive cost for a targeted attack against a specific node operator.
- **No cryptographic or consensus barrier**: The transaction is fully valid; it passes all script verification, capacity checks, and pool admission rules.
- **Repeatability**: The attacker can re-submit after each block to maintain the condition.

---

### Recommendation

1. **Cap `max_bucket_index`** in `max_bucket_index_by_fee_rate` to a hard constant (e.g., 10,000), matching the practical fee rate range the algorithm is designed for.
2. **Add a `max_fee_rate` guard** in `do_estimate`: if `max_fee_rate` exceeds a reasonable threshold (e.g., 10× the highest historically observed fee rate), clamp it before computing the bucket index.
3. **Optionally**, add a `max_fee_rate` admission check in `check_tx_fee` to reject transactions with implausibly high fee rates.

---

### Proof of Concept

```rust
// Compute bucket index for fee_rate = 10^15
let fee_rate = FeeRate::from_u64(1_000_000_000_000_000u64); // 10^15
let max_bucket_index = Algorithm::max_bucket_index_by_fee_rate(fee_rate);
// max_bucket_index ≈ 10_000_000_000 (10 billion)
// vec![0u64; max_bucket_index + 1] → 80 GB allocation → OOM abort

// Attacker transaction parameters:
// - size: ~200 bytes (minimal tx)
// - fee: 100_000 CKB = 10^13 shannons
// - weight: 200
// - fee_rate = 10^13 * 1000 / 200 = 5 × 10^13
// - max_bucket_index ≈ 5 × 10^8 → 4 GB allocation
```

Submit via `send_transaction` RPC, then call `estimate_fee_rate` — the node aborts.

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

**File:** tx-pool/src/util.rs (L44-52)
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
```

**File:** resource/ckb.toml (L306-308)
```text
# # [fee_estimator]
# # Specifies the fee estimates algorithm. Current algorithms: ConfirmationFraction, WeightUnitsFlow.
# # algorithm = "WeightUnitsFlow"
```
