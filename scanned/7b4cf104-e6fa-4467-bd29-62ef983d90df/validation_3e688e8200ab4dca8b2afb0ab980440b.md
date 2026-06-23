### Title
Hardcoded `MAX_BLOCK_BYTES` Compile-Time Constant Used in Fee Estimator Instead of Runtime Consensus Value — (File: `util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

The `WeightUnitsFlow` fee estimator algorithm hardcodes the compile-time constant `MAX_BLOCK_BYTES` (597,000 bytes) when computing how much transaction weight can be removed from the mempool per block. This constant is imported directly from `ckb_chain_spec::consensus` rather than read from the live consensus object. When a node's actual `max_block_bytes` consensus parameter differs from this constant (e.g., on testnets or custom chains), the fee estimator produces systematically incorrect fee rate estimates for all RPC callers invoking `estimate_fee_rate`.

---

### Finding Description

In `util/fee-estimator/src/estimator/weight_units_flow.rs`, the `do_estimate` function computes `removed_weight` — the amount of transaction weight that miners can remove from the mempool per block — using a hardcoded compile-time constant:

```rust
// util/fee-estimator/src/estimator/weight_units_flow.rs, line 57
use ckb_chain_spec::consensus::MAX_BLOCK_BYTES;

// line 284
let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
```

`MAX_BLOCK_BYTES` is defined as a compile-time constant:

```rust
// spec/src/consensus.rs, line 83
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT; // 597 * 1000 = 597,000
```

However, `max_block_bytes` is a **configurable consensus parameter** on the `Consensus` struct, accessible at runtime via `snapshot.consensus().max_block_bytes()`. The fallback fee estimator in `tx-pool/src/pool.rs` correctly uses the runtime value:

```rust
// tx-pool/src/pool.rs, line 567
self.snapshot.consensus().max_block_bytes() as usize,
```

The `WeightUnitsFlow` algorithm never receives a `Snapshot` or `Consensus` reference, so it cannot read the actual runtime `max_block_bytes`. It is permanently bound to the compile-time constant regardless of what the node's chain spec actually configures.

Additionally, the `LOWEST_FEE_RATE` constant used as a fallback return value in the same algorithm is hardcoded to 1000 shannons/KB and does not reflect the node's actual `min_fee_rate` configuration:

```rust
// util/fee-estimator/src/constants.rs, line 16
pub(crate) const LOWEST_FEE_RATE: FeeRate = FeeRate::from_u64(1000);

// weight_units_flow.rs, line 209
return Ok(constants::LOWEST_FEE_RATE);
```

---

### Impact Explanation

The `removed_weight` value is the central variable in the fee estimation decision loop:

```rust
// weight_units_flow.rs, lines 279–297
for bucket_index in 1..=max_bucket_index {
    let current_weight = current_weight_buckets[bucket_index];
    let added_weight = flow_speed_buckets[bucket_index] * target_blocks;
    let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
    let passed = current_weight + added_weight <= removed_weight;
    if passed {
        let fee_rate = Self::lowest_fee_rate_by_bucket_index(bucket_index);
        return Ok(fee_rate);
    }
}
```

If the actual `max_block_bytes` is **larger** than the constant (e.g., a chain configured with larger blocks), `removed_weight` is underestimated, causing the algorithm to believe blocks fill up faster than they actually do. This results in **overestimated fee rates** — users overpay unnecessarily.

If the actual `max_block_bytes` is **smaller** than the constant, `removed_weight` is overestimated, causing the algorithm to believe blocks have more capacity than they do. This results in **underestimated fee rates** — users submit transactions with fees too low to be confirmed within the expected target, causing delayed or failed inclusion.

When the pool is empty and `LOWEST_FEE_RATE` (1000) is returned but the node's `min_fee_rate` is configured higher (e.g., 2000), transactions built using the estimate are immediately rejected by the tx-pool with `LowFeeRate`.

---

### Likelihood Explanation

On CKB mainnet, `max_block_bytes` defaults to `MAX_BLOCK_BYTES` (597,000 bytes), so there is no divergence and no impact on mainnet. However:

- On CKB testnet or any custom chain spec where `max_block_bytes` is configured differently, every call to `estimate_fee_rate` using the `WeightUnitsFlow` algorithm produces a systematically biased result.
- Any node operator who sets `min_fee_rate` to a non-default value (a documented, supported configuration) will receive fee estimates from the `WeightUnitsFlow` fallback path that may be below the actual pool minimum.
- The entry path is fully unprivileged: any RPC caller can invoke `estimate_fee_rate` to trigger this code path.

---

### Recommendation

Pass the runtime consensus `max_block_bytes` value into the `WeightUnitsFlow::estimate_fee_rate` function rather than importing the compile-time constant. Similarly, pass the node's configured `min_fee_rate` as the floor for fee estimates instead of using the hardcoded `LOWEST_FEE_RATE` constant. The fallback algorithm in `TxPool::estimate_fee_rate` already demonstrates the correct pattern by reading `self.snapshot.consensus().max_block_bytes()` at runtime.

---

### Proof of Concept

**Root cause — hardcoded constant import:** [1](#0-0) 

**Hardcoded constant used in fee estimation decision:** [2](#0-1) 

**Compile-time constant definition (not the runtime consensus value):** [3](#0-2) 

**Correct pattern: fallback algorithm reads runtime consensus value:** [4](#0-3) 

**Hardcoded `LOWEST_FEE_RATE` returned when pool is empty:** [5](#0-4) 

**`LOWEST_FEE_RATE` constant definition (does not reflect node's `min_fee_rate`):** [6](#0-5) 

**Node's actual `min_fee_rate` is configurable and defaults to 1000 but can differ:** [7](#0-6) 

**RPC entry point that triggers the `WeightUnitsFlow` algorithm:** [8](#0-7)

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L57-57)
```rust
use ckb_chain_spec::consensus::MAX_BLOCK_BYTES;
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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L279-297)
```rust
        for bucket_index in 1..=max_bucket_index {
            let current_weight = current_weight_buckets[bucket_index];
            let added_weight = flow_speed_buckets[bucket_index] * target_blocks;
            // Note: blocks are not full even there are many pending transactions,
            // since `MAX_BLOCK_PROPOSALS_LIMIT = 1500`.
            let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
            let passed = current_weight + added_weight <= removed_weight;
            ckb_logger::trace!(
                ">>> bucket[{}]: {}; {} + {} - {}",
                bucket_index,
                passed,
                current_weight,
                added_weight,
                removed_weight
            );
            if passed {
                let fee_rate = Self::lowest_fee_rate_by_bucket_index(bucket_index);
                return Ok(fee_rate);
            }
```

**File:** spec/src/consensus.rs (L83-84)
```rust
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** tx-pool/src/pool.rs (L564-571)
```rust
        let fee_rate = self.pool_map.estimate_fee_rate(
            (target_to_be_committed - self.snapshot.consensus().tx_proposal_window().closest())
                as usize,
            self.snapshot.consensus().max_block_bytes() as usize,
            self.snapshot.consensus().max_block_cycles(),
            self.config.min_fee_rate,
        );
        Ok(fee_rate)
```

**File:** util/fee-estimator/src/constants.rs (L15-16)
```rust
/// Lowest fee rate.
pub(crate) const LOWEST_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-10)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
```

**File:** tx-pool/src/process.rs (L945-970)
```rust
    pub(crate) async fn estimate_fee_rate(
        &self,
        estimate_mode: EstimateMode,
        enable_fallback: bool,
    ) -> Result<FeeRate, AnyError> {
        let all_entry_info = self.tx_pool.read().await.get_all_entry_info();
        match self
            .fee_estimator
            .estimate_fee_rate(estimate_mode, all_entry_info)
        {
            Ok(fee_rate) => Ok(fee_rate),
            Err(err) => {
                if enable_fallback {
                    let target_blocks =
                        FeeEstimator::target_blocks_for_estimate_mode(estimate_mode);
                    self.tx_pool
                        .read()
                        .await
                        .estimate_fee_rate(target_blocks)
                        .map_err(Into::into)
                } else {
                    Err(err.into())
                }
            }
        }
    }
```
