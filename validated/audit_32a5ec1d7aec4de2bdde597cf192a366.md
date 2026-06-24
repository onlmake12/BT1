All claims verified against the actual source code. Here is the validated audit report:

---

Audit Report

## Title
Unbounded Heap Allocation via Extreme Fee Rate in `do_estimate` — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

## Summary
`do_estimate` derives `max_bucket_index` directly from the highest fee rate among all pending/proposed transactions via `max_bucket_index_by_fee_rate`, then allocates two `Vec<u64>` of length `max_bucket_index + 1` with no upper-bound guard. Because `FeeRate::calculate` uses `saturating_mul` and the tx-pool enforces only a minimum fee rate, an attacker can submit a single valid transaction with an extreme fee and minimal weight, then call `estimate_fee_rate` to trigger a multi-gigabyte heap allocation, crashing the node with OOM.

## Finding Description

**Root cause — `max_bucket_index_by_fee_rate` is unbounded.**

The final match arm applies for any fee rate above 2,000,000 shannons/KW:

```rust
x => (x + t * 11_500) / (100 * t)   // t = 1000
```

For a fee rate of 1.25 × 10^13 (achievable by burning ~25,000 CKB in a 200-weight tx), this yields `index ≈ 125,000,000`. No cap is applied anywhere. [1](#0-0) 

**Step 1 — Fee rate is unbounded from above.**

`FeeRate::calculate` uses `saturating_mul(KW)`, so the maximum representable fee rate is `u64::MAX`. [2](#0-1) 

`check_tx_fee` enforces only a *minimum* fee rate; there is no maximum. [3](#0-2) 

**Step 2 — `max_fee_rate` is taken directly from the attacker's tx.**

`estimate_fee_rate` sorts all pending/proposed transactions by fee rate descending. `do_estimate` takes `max_fee_rate` from the first (highest) element — the attacker's transaction. [4](#0-3) [5](#0-4) 

**Step 3 — Two uncapped `Vec` allocations of that size.**

Both `current_weight_buckets` and `flow_speed_buckets` are allocated as `vec![0u64; max_bucket_index + 1]` with no guard between the index computation and the allocation. [6](#0-5) [7](#0-6) 

## Impact Explanation

A single `estimate_fee_rate` RPC call after submitting a high-fee transaction causes the node process to attempt an allocation proportional to the fee rate, crashing with OOM. The node is completely unavailable until restarted, and the attack is repeatable. This matches the allowed CKB bounty impact: **"Vulnerabilities which could easily crash a CKB node" (High, 10001–15000 points)**. If applied simultaneously to a significant fraction of the network, it escalates to **"Vulnerabilities which could easily crash the whole CKB network" (Critical, 15001–25000 points)**.

| Fee burned | Weight (bytes) | FeeRate (sh/KW) | `max_bucket_index` | Allocation |
|---|---|---|---|---|
| 25,000 CKB | 200 | 1.25 × 10^13 | ~1.25 × 10^8 | ~1 GB |
| 200,000 CKB | 200 | 1 × 10^14 | ~10^9 | ~8 GB |
| 1,000,000 CKB | 200 | 5 × 10^14 | ~5 × 10^9 | ~40 GB |

## Likelihood Explanation

- No privileged access is required. Any entity that can submit a valid CKB transaction and reach the RPC endpoint can trigger this.
- The `estimate_fee_rate` RPC is accessible when the `Experiment` RPC module is enabled.
- No cap exists anywhere between tx submission and the `vec!` call.
- The attack is repeatable: after the node restarts, the attacker's tx may still be in the pool (or can be resubmitted), and a single RPC call re-triggers the crash.
- Cost is non-zero but affordable: ~25,000 CKB is sufficient to cause a ~1 GB allocation on a constrained node.

## Recommendation

Add a hard cap on `max_bucket_index` before any allocation in `do_estimate`:

```rust
const MAX_BUCKET_INDEX: usize = 2000; // covers fee rates up to ~200,000,000 shannons/KW
let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate)
    .min(MAX_BUCKET_INDEX);
```

Additionally, harden `max_bucket_index_by_fee_rate` itself to return a bounded value regardless of input, so the invariant is enforced at the source rather than only at the call site. [1](#0-0) 

## Proof of Concept

Minimal unit test (no OOM should occur after the fix):

```rust
#[test]
fn test_max_bucket_index_is_bounded() {
    // 25,000 CKB fee, 200 weight → fee_rate = 1.25 × 10^13 shannons/KW
    let fee_rate = FeeRate::from_u64(12_500_000_000_000u64);
    let index = Algorithm::max_bucket_index_by_fee_rate(fee_rate);
    // Without fix: index ≈ 125_000_000 → vec![0u64; 125_000_001] ≈ 1 GB → OOM
    assert!(index <= 2000, "index {} is unbounded and will OOM", index);
}
```

Call sequence:
`submit_tx(high fee, small weight)` → `accept_tx` → tx enters pool → `estimate_fee_rate` RPC → `get_all_entry_info` → `do_estimate` → `max_bucket_index_by_fee_rate(extreme_fee_rate)` → `vec![0u64; huge_index+1]` → **OOM crash** [8](#0-7)

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L164-185)
```rust
    pub fn estimate_fee_rate(
        &self,
        target_blocks: BlockNumber,
        all_entry_info: TxPoolEntryInfo,
    ) -> Result<FeeRate, Error> {
        if !self.is_ready {
            return Err(Error::NotReady);
        }

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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L218-220)
```rust
        let current_weight_buckets = {
            let mut buckets = vec![0u64; max_bucket_index + 1];
            let mut index_curr = max_bucket_index;
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
