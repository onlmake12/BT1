Now I have all the information needed. Let me do the arithmetic precisely.

### Title
Unbounded Vector Allocation in `do_estimate` via Extreme Fee Rate Transaction — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

`max_bucket_index_by_fee_rate` has no upper-bound cap on its return value. `do_estimate` uses that return value directly as the size of two heap-allocated `Vec<u64>` buffers. An attacker who submits a transaction with a sufficiently high fee rate causes the next `estimate_fee_rate` RPC call to attempt a multi-gigabyte (or larger) allocation, crashing the node with OOM.

---

### Finding Description

`FEE_RATE_UNIT = 1000`. For any `fee_rate > 2_000_000`, `max_bucket_index_by_fee_rate` computes:

```
index = (x + 1_000 * 11_500) / (100 * 1_000)
      = (x + 11_500_000) / 100_000
```

with no ceiling. [1](#0-0) 

`do_estimate` then allocates two vectors of that size unconditionally:

```rust
let mut buckets = vec![0u64; max_bucket_index + 1];   // line 219
...
let mut buckets = vec![0u64; max_bucket_index + 1];   // line 248
``` [2](#0-1) [3](#0-2) 

`FeeRate::calculate` is defined as `fee.as_u64().saturating_mul(1000) / weight`. [4](#0-3) 

For a minimal transaction (weight ≈ 100 weight-units) with fee = 10^13 shannons (100,000 CKB):

```
fee_rate = 10^13 * 1000 / 100 = 10^14
index    = (10^14 + 11_500_000) / 100_000 ≈ 1_000_000_000
vec size = 1_000_000_001 × 8 bytes ≈ 8 GB  →  OOM
```

For fee_rate near `u64::MAX - 11_500_000` (achievable when `fee * 1000` saturates and weight is small):

```
index ≈ 184_467_440_737_095  →  ~1.47 PB allocation
```

Note: `fee_rate = u64::MAX` itself wraps on `x + 11_500_000` in release mode and produces index 114 (safe), but this is a single-point edge case; the entire range `[2_000_001, u64::MAX - 11_500_001]` produces unbounded indices.

The `estimate_fee_rate` path reads all pending and proposed transactions from the tx-pool snapshot and passes them directly into `do_estimate` with no fee-rate sanitization: [5](#0-4) 

There is no `max_fee_rate` guard anywhere in tx-pool admission or in the fee estimator. [6](#0-5) 

---

### Impact Explanation

A single `estimate_fee_rate` RPC call after one extreme-fee transaction is in the mempool causes the process allocator to request gigabytes (or more) of contiguous memory. On Linux this results in either an immediate `SIGKILL` from the OOM killer or `std::alloc` aborting the process. Either way the node crashes. The attacker's transaction is never mined (the node dies first), so the attacker does not lose their CKB. The attack is therefore repeatable at near-zero cost after the initial UTXO is created.

---

### Likelihood Explanation

- The `WeightUnitsFlow` estimator is an opt-in configuration, so only nodes that enable it are affected.
- The attacker needs to own enough CKB to create a transaction whose fee rate exceeds ~10^12 shannons/KW (roughly 1,000–10,000 CKB depending on minimum tx weight), which is a realistic holding.
- The `estimate_fee_rate` RPC is a standard, unauthenticated endpoint that node operators and tooling call routinely.
- No special timing, hashpower, or privileged access is required.

---

### Recommendation

Add a hard cap in `max_bucket_index_by_fee_rate`:

```rust
fn max_bucket_index_by_fee_rate(fee_rate: FeeRate) -> usize {
    const MAX_BUCKET_INDEX: usize = 1000; // or another safe constant
    let t = FEE_RATE_UNIT;
    let index = match fee_rate.as_u64() {
        // ... existing arms ...
        x => (x + t * 11_500) / (100 * t),
    };
    (index as usize).min(MAX_BUCKET_INDEX)
}
```

Alternatively, cap `max_fee_rate` in `do_estimate` before computing `max_bucket_index`, or reject transactions whose computed fee rate exceeds a safe threshold at tx-pool admission time.

---

### Proof of Concept

1. Enable `WeightUnitsFlow` fee estimator in node config.
2. Wait for the estimator to become ready (`is_ready = true`, requires syncing past `historical_blocks`).
3. Submit a transaction via `send_transaction` RPC with:
   - inputs consuming a UTXO worth ≥ 100,000 CKB
   - outputs worth ~0 CKB (fee ≈ 100,000 CKB = 10^13 shannons)
   - minimal serialized size (~100 bytes, 0 cycles → weight ≈ 100)
   - computed `fee_rate ≈ 10^14`
4. Call `estimate_fee_rate` RPC with any `EstimateMode`.
5. `do_estimate` computes `max_bucket_index ≈ 10^9`, attempts `vec![0u64; 10^9 + 1]` (8 GB), node crashes.

The attacker's UTXO is not consumed (node crashes before the block is committed), so the attack can be repeated.

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L173-185)
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

**File:** util/fee-estimator/src/estimator/mod.rs (L92-105)
```rust
    pub fn estimate_fee_rate(
        &self,
        estimate_mode: EstimateMode,
        all_entry_info: TxPoolEntryInfo,
    ) -> Result<FeeRate, Error> {
        let target_blocks = Self::target_blocks_for_estimate_mode(estimate_mode);
        match self {
            Self::Dummy => Err(Error::Dummy),
            Self::ConfirmationFraction(algo) => algo.read().estimate_fee_rate(target_blocks),
            Self::WeightUnitsFlow(algo) => {
                algo.read().estimate_fee_rate(target_blocks, all_entry_info)
            }
        }
    }
```
