### Title
`weight_units_flow` Fee Estimator Uses Hardcoded `MAX_BLOCK_BYTES` Instead of Live Consensus Value, Causing Systematic Fee-Rate Underestimation — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

The `WeightUnitsFlow` fee-estimation algorithm hard-codes the chain's block-byte capacity as the constant `MAX_BLOCK_BYTES` imported from `ckb_chain_spec::consensus`. The sibling fallback estimator in `tx-pool/src/component/pool_map.rs` correctly receives `consensus.max_block_bytes()` as a live parameter. When the two values diverge — which happens on every testnet, staging network, or custom chain spec — the `WeightUnitsFlow` algorithm over-estimates how much weight each block removes from the mempool, causing it to return a fee rate that is systematically too low. Any RPC caller who relies on `estimate_fee_rate` with this algorithm will submit transactions with insufficient fees and miss their target confirmation window.

---

### Finding Description

`weight_units_flow.rs` imports the constant directly:

```rust
use ckb_chain_spec::consensus::MAX_BLOCK_BYTES;
``` [1](#0-0) 

Inside `do_estimate`, the per-block throughput is computed as:

```rust
let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
let passed = current_weight + added_weight <= removed_weight;
``` [2](#0-1) 

`MAX_BLOCK_BYTES` is a compile-time constant that encodes the **mainnet** block-byte limit. It is never read from the running node's consensus object.

By contrast, the fallback estimator in `pool_map.rs` receives the live value as a function argument:

```rust
pub(crate) fn estimate_fee_rate(
    &self,
    mut target_blocks: usize,
    max_block_bytes: usize,       // ← passed from consensus at call time
    max_block_cycles: Cycle,
    min_fee_rate: FeeRate,
) -> FeeRate {
``` [3](#0-2) 

and is called with the actual consensus value:

```rust
self.pool_map.estimate_fee_rate(
    ...
    self.snapshot.consensus().max_block_bytes() as usize,
    ...
)
``` [4](#0-3) 

The `estimate_fee_rate` RPC dispatches to `WeightUnitsFlow` when that algorithm is configured, falling back to the pool-map estimator only on `Error::LackData` / `Error::NotReady`:

```rust
match self.fee_estimator.estimate_fee_rate(estimate_mode, all_entry_info) {
    Ok(fee_rate) => Ok(fee_rate),
    Err(err) => {
        if enable_fallback {
            ...self.tx_pool.read().await.estimate_fee_rate(target_blocks)...
``` [5](#0-4) 

So when `WeightUnitsFlow` has enough historical data to return `Ok(fee_rate)`, the fallback is never reached and the hardcoded constant is used unchecked.

The structural parallel to TRST-M-10 is exact: MozBridge encoded only the message-type tag when estimating LayerZero fees, omitting the full `Snapshot` struct payload. Here, `WeightUnitsFlow` encodes only the mainnet block-byte limit when estimating throughput, omitting the actual chain's configured limit.

---

### Impact Explanation

`removed_weight` represents how many weight units each mined block drains from the mempool. If `MAX_BLOCK_BYTES` is larger than the chain's actual `max_block_bytes`:

- `removed_weight` is over-estimated.
- The algorithm concludes that a lower fee rate is sufficient to clear the mempool within `target_blocks`.
- The returned fee rate is **too low**.
- Transactions submitted at that rate will not be confirmed within the advertised window; they stall in the pending pool until the operator manually bumps the fee or the transaction expires.

On a testnet or staging network where `max_block_bytes` is intentionally reduced (a common configuration for stress-testing), the gap between the constant and the real limit can be large, making the underestimation severe.

---

### Likelihood Explanation

- The `WeightUnitsFlow` algorithm is one of the selectable fee estimators exposed via the `estimate_fee_rate` RPC endpoint.
- Any RPC caller (wallet, dApp, exchange) that calls `estimate_fee_rate` on a non-mainnet node running `WeightUnitsFlow` is affected.
- No special privileges are required; the endpoint is publicly accessible.
- The bug is silent: the RPC returns a plausible-looking fee rate with no error, so callers have no indication the estimate is wrong.

---

### Recommendation

Pass the live consensus value into `do_estimate` instead of importing the compile-time constant:

```rust
// In Algorithm::estimate_fee_rate, thread max_block_bytes through:
pub fn estimate_fee_rate(
    &self,
    target_blocks: BlockNumber,
    all_entry_info: TxPoolEntryInfo,
    max_block_bytes: u64,          // ← add parameter
) -> Result<FeeRate, Error> { ... }

// In do_estimate, replace:
let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
// with:
let removed_weight = (max_block_bytes * 85 / 100) * target_blocks;
```

Propagate the parameter from `TxPool::estimate_fee_rate` → `FeeEstimator::estimate_fee_rate` → `Algorithm::estimate_fee_rate`, sourcing it from `snapshot.consensus().max_block_bytes()`, exactly as the fallback estimator already does.

---

### Proof of Concept

1. Start a CKB node with a chain spec where `max_block_bytes` is set to a value smaller than the mainnet `MAX_BLOCK_BYTES` constant (e.g., half the mainnet value).
2. Configure the node to use the `WeightUnitsFlow` fee estimator.
3. Fill the mempool with transactions at various fee rates.
4. Call `estimate_fee_rate` via RPC with `estimate_mode: "high_priority"`.
5. Observe that the returned fee rate is lower than what is actually required to be included within the target block count, because `removed_weight` was computed using the larger mainnet constant rather than the node's actual `max_block_bytes`.
6. Submit a transaction at the returned fee rate and observe it is not confirmed within the advertised window.

The root cause is at: [1](#0-0) [2](#0-1)

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L57-57)
```rust
use ckb_chain_spec::consensus::MAX_BLOCK_BYTES;
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L284-285)
```rust
            let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
            let passed = current_weight + added_weight <= removed_weight;
```

**File:** tx-pool/src/component/pool_map.rs (L334-340)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
```

**File:** tx-pool/src/pool.rs (L564-570)
```rust
        let fee_rate = self.pool_map.estimate_fee_rate(
            (target_to_be_committed - self.snapshot.consensus().tx_proposal_window().closest())
                as usize,
            self.snapshot.consensus().max_block_bytes() as usize,
            self.snapshot.consensus().max_block_cycles(),
            self.config.min_fee_rate,
        );
```

**File:** tx-pool/src/process.rs (L951-968)
```rust
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
```
