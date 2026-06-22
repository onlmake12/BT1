Now I have all the information needed to analyze this vulnerability. Let me trace through the exact math.

### Title
Unbounded Heap Allocation via Extreme Fee Rate in `do_estimate` — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

An unprivileged attacker can submit a single valid transaction with a very high fee and minimal serialized weight into the tx-pool, then call the `estimate_fee_rate` RPC. This causes `do_estimate` to invoke `max_bucket_index_by_fee_rate` on the resulting extreme `FeeRate` value, producing a bucket index on the order of 10^8–10^14, and then trigger `vec![0u64; max_bucket_index + 1]` to attempt a multi-gigabyte or petabyte heap allocation, crashing the node with OOM.

---

### Finding Description

The vulnerability lives in `do_estimate` inside `weight_units_flow.rs`. The function derives `max_bucket_index` directly from the highest fee rate among all current pending/proposed transactions, then allocates two `Vec<u64>` of that size with no upper bound check.

**Step 1 — Fee rate is unbounded from above.**

`FeeRate::calculate` uses `saturating_mul`, so the maximum representable fee rate is `u64::MAX ≈ 1.84 × 10^19` shannons/KW: [1](#0-0) 

The tx-pool admission check (`check_tx_fee`) only enforces a *minimum* fee rate — there is no maximum: [2](#0-1) 

**Step 2 — `max_bucket_index_by_fee_rate` grows linearly with fee rate, without a cap.**

For any fee rate above 2,000,000 shannons/KW, the last branch applies:

```
index = (x + 11_500_000) / 100_000
```

For `x = u64::MAX ≈ 1.84 × 10^19`, this yields `index ≈ 1.84 × 10^14` (184 trillion). [3](#0-2) 

**Step 3 — Two uncapped `Vec` allocations of that size.**

`do_estimate` allocates `current_weight_buckets` and `flow_speed_buckets`, both of length `max_bucket_index + 1`, with no guard: [4](#0-3) [5](#0-4) 

**Step 4 — The extreme fee rate comes directly from the attacker's tx in the pool.**

`estimate_fee_rate` builds `sorted_current_txs` from the live tx-pool state and passes it to `do_estimate`. The `max_fee_rate` is taken from the first (highest) element: [6](#0-5) [7](#0-6) 

---

### Impact Explanation

| Fee burned | Weight (bytes) | FeeRate (shannons/KW) | `max_bucket_index` | Allocation |
|---|---|---|---|---|
| 25,000 CKB (2.5 × 10^12 sh) | 200 | 1.25 × 10^13 | ~1.25 × 10^8 | ~1 GB |
| 200,000 CKB (2 × 10^13 sh) | 200 | 1 × 10^14 | ~10^9 | ~8 GB |
| 1,000,000 CKB (10^14 sh) | 200 | 5 × 10^14 | ~5 × 10^9 | ~40 GB |

A single RPC call after submitting the transaction causes the node process to attempt the allocation and crash with OOM. The node is completely unavailable until restarted, and the attack is repeatable.

---

### Likelihood Explanation

- **No privileged access required.** Any entity that can submit a valid CKB transaction and reach the RPC endpoint can trigger this.
- **Cost is non-zero but affordable.** Burning ~25,000 CKB (~$250–500 at current prices) is sufficient to cause a 1 GB allocation on a typical node. Burning ~200,000 CKB (~$2,000–4,000) reliably causes OOM on an 8 GB node.
- **The `estimate_fee_rate` RPC is unauthenticated** and publicly accessible when the `Experiment` RPC module is enabled.
- **No cap exists anywhere** between tx submission and the `vec!` call.

---

### Recommendation

Add a hard cap on `max_bucket_index` before any allocation in `do_estimate`. For example:

```rust
const MAX_BUCKET_INDEX: usize = 2000; // covers fee rates up to ~200,000,000 shannons/KW
let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate)
    .min(MAX_BUCKET_INDEX);
```

Alternatively, cap the fee rate used for bucket index computation at a protocol-defined maximum (e.g., 2,000,000 shannons/KW, which already covers all realistic fee rates). The `max_bucket_index_by_fee_rate` function itself should also be hardened to return a bounded value regardless of input.

---

### Proof of Concept

```rust
// Minimal unit test — no OOM should occur
use ckb_types::core::{Capacity, FeeRate, tx_pool::TxEntryInfo};
use weight_units_flow::Algorithm;

let info = TxEntryInfo {
    fee: Capacity::shannons(2_500_000_000_000u64), // 25,000 CKB
    size: 200,
    cycles: 0,
    // ... other fields zeroed
};
// FeeRate::calculate(2.5e12 shannons, 200 weight) = 1.25e13 shannons/KW
// max_bucket_index_by_fee_rate(1.25e13) ≈ 125_000_000
// vec![0u64; 125_000_001] = ~1 GB allocation → OOM on constrained nodes

let fee_rate = FeeRate::calculate(Capacity::shannons(2_500_000_000_000u64), 200);
let index = Algorithm::max_bucket_index_by_fee_rate(fee_rate);
assert!(index < 10_000, "index {} is unbounded and will OOM", index); // FAILS
```

The call sequence is:
`submit_tx (high fee, small weight)` → `accept_tx` → tx enters pool → `estimate_fee_rate` RPC → `get_all_entry_info` → `do_estimate` → `max_bucket_index_by_fee_rate(extreme_fee_rate)` → `vec![0u64; huge_index+1]` → **OOM crash**.

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
