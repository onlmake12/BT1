### Title
`estimate_fee_rate` Block-Boundary Reset Uses Last Entry's Size Instead of Zero, Inflating Fee Rate Estimates — (`tx-pool/src/component/pool_map.rs`)

### Summary

`PoolMap::estimate_fee_rate` simulates filling multiple virtual blocks with pending transactions to estimate the fee rate needed for inclusion in the `target_blocks`-th block. When a block boundary is detected, the code resets the running byte/cycle counters to the **current entry's own size and cycles** rather than to zero. This double-counts the boundary entry — it is counted in both the block that just overflowed and the next block — causing the next simulated block to appear partially full from the start. The result is an inflated fee rate estimate whenever `target_blocks > 1`.

This is structurally identical to the AuraLocker M-04 bug: a shortcut path uses a single element's value (the last/boundary entry's size) instead of the correct accumulated/total value (zero, for a fresh block start).

---

### Finding Description

In `tx-pool/src/component/pool_map.rs`, `estimate_fee_rate` iterates over pool entries sorted by fee rate (highest first) and simulates block packing:

```rust
for entry in iter {
    current_block_bytes += entry.inner.size;
    current_block_cycles += entry.inner.cycles;
    if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
        target_blocks -= 1;
        if target_blocks == 0 {
            return entry.inner.fee_rate();
        }
        current_block_bytes = entry.inner.size;   // ← BUG
        current_block_cycles = entry.inner.cycles; // ← BUG
    }
}
``` [1](#0-0) 

The entry that triggers the overflow condition has already been added to `current_block_bytes` and `current_block_cycles` on lines 346–347. After the block boundary is recorded, the reset on lines 353–354 sets the counters to that same entry's size and cycles — meaning the entry is counted **twice**: once in the block that just overflowed, and again as the first entry of the next block.

The correct reset is to zero, so the next simulated block starts empty:

```rust
current_block_bytes = 0;
current_block_cycles = 0;
```

This function is called as the fallback fee estimator from `tx-pool/src/pool.rs`: [2](#0-1) 

The `target_blocks` argument passed to `pool_map.estimate_fee_rate` is `target_to_be_committed - closest`. On mainnet `closest = 2`, so any call with `target_to_be_committed >= 4` produces `target_blocks >= 2` and triggers the bug. The fallback path is reached whenever the primary `FeeEstimator` (ConfirmationFraction / WeightUnitsFlow) returns `Error::LackData` or `Error::NotReady` — a common condition during node startup, after IBD, or when the node has been running for fewer blocks than the estimator's historical window. [3](#0-2) 

---

### Impact Explanation

Because the boundary entry is double-counted, the next simulated block appears to start with some bytes/cycles already consumed. This causes the simulated block to fill up faster (at a higher-fee-rate entry), so the returned fee rate is **higher than the correct value**. Any wallet, tool, or script that calls `estimate_fee_rate` with a multi-block target and relies on the fallback path will systematically overpay transaction fees. The magnitude of overpayment grows with the number of target blocks and the size of the boundary entry relative to the block limit.

---

### Likelihood Explanation

The `estimate_fee_rate` RPC is an experimental but documented endpoint reachable by any unprivileged local or remote RPC caller. The fallback path (`pool_map.estimate_fee_rate`) is activated whenever the primary estimator lacks sufficient historical data — a routine condition after node restart or during initial sync. Any caller requesting `LowPriority`, `MediumPriority`, or `NoPriority` modes (which map to `target_blocks` values well above 1) during such a window will receive an inflated estimate.

---

### Recommendation

Replace the double-counting reset with a zero reset so each simulated block starts empty:

```rust
// After recording the block boundary:
current_block_bytes = 0;
current_block_cycles = 0;
``` [4](#0-3) 

---

### Proof of Concept

Consider `max_block_bytes = 1000`, `target_blocks = 2`, and three entries sorted by fee rate (highest first): **A** (600 bytes), **B** (500 bytes), **C** (400 bytes).

**Current (buggy) behavior:**
- Iteration A: `current_block_bytes = 600`. No overflow.
- Iteration B: `current_block_bytes = 1100 ≥ 1000`. Block 1 full. `target_blocks = 1`. Reset: `current_block_bytes = 500` (B's size).
- Iteration C: `current_block_bytes = 500 + 400 = 900`. No overflow.
- Loop ends. Return `min_fee_rate`.

But B was already counted in block 1 (it caused the overflow) and is also counted as the first 500 bytes of block 2. Block 2 is artificially pre-loaded with 500 bytes.

**Correct behavior (reset to 0):**
- Iteration A: `current_block_bytes = 600`. No overflow.
- Iteration B: `current_block_bytes = 1100 ≥ 1000`. Block 1 full. `target_blocks = 1`. Reset: `current_block_bytes = 0`.
- Iteration C: `current_block_bytes = 400`. No overflow.
- Loop ends. Return `min_fee_rate`.

In a scenario where block 2 would overflow under the buggy reset (because it starts pre-loaded), the function returns a higher fee rate than warranted. The test fixture at lines 30–33 of `tx-pool/src/component/tests/estimate.rs` exercises `target_blocks = 2` and accepts the inflated result as the expected value, masking the bug. [5](#0-4) [6](#0-5)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L334-359)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
    }
```

**File:** tx-pool/src/pool.rs (L557-572)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        target_to_be_committed: BlockNumber,
    ) -> Result<FeeRate, FeeEstimatorError> {
        if !(3..=131).contains(&target_to_be_committed) {
            return Err(FeeEstimatorError::NoProperFeeRate);
        }
        let fee_rate = self.pool_map.estimate_fee_rate(
            (target_to_be_committed - self.snapshot.consensus().tx_proposal_window().closest())
                as usize,
            self.snapshot.consensus().max_block_bytes() as usize,
            self.snapshot.consensus().max_block_cycles(),
            self.config.min_fee_rate,
        );
        Ok(fee_rate)
    }
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

**File:** tx-pool/src/component/tests/estimate.rs (L30-33)
```rust
    assert_eq!(
        FeeRate::from_u64(1016),
        pool.estimate_fee_rate(2, 5000, Cycle::MAX, FeeRate::from_u64(1))
    );
```
