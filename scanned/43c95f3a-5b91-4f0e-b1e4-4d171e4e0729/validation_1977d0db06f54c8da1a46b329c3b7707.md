### Title
Unbounded Iteration over All Tx-Pool Entries in `PoolMap::estimate_fee_rate` Enables CPU Exhaustion DoS - (File: `tx-pool/src/component/pool_map.rs`)

### Summary
`PoolMap::estimate_fee_rate` iterates over every entry in the transaction pool in a single pass with no per-call iteration cap. When the pool does not contain enough transactions to fill the requested number of target blocks, the loop exhausts the entire pool without early termination. Because the pool is bounded only by total byte size (default 180 MB), an attacker who fills the pool with many minimum-size transactions can force each `estimate_fee_rate` RPC call to scan hundreds of thousands of entries while holding the pool read-lock, causing sustained CPU pressure and blocking concurrent write operations.

### Finding Description
In `tx-pool/src/component/pool_map.rs`, `estimate_fee_rate` iterates over all pool entries sorted by score:

```rust
pub(crate) fn estimate_fee_rate(
    &self,
    mut target_blocks: usize,
    max_block_bytes: usize,
    max_block_cycles: Cycle,
    min_fee_rate: FeeRate,
) -> FeeRate {
    let iter = self.entries.iter_by_score().rev();
    for entry in iter {                          // ← iterates ALL entries
        current_block_bytes += entry.inner.size;
        current_block_cycles += entry.inner.cycles;
        if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
            target_blocks -= 1;
            if target_blocks == 0 {
                return entry.inner.fee_rate();   // only early exit
            }
            ...
        }
    }
    min_fee_rate                                 // reached when pool < target_blocks blocks
}
``` [1](#0-0) 

The sole early-exit condition is `target_blocks == 0`, which fires only after the pool contains enough transactions to fill `target_blocks` full blocks. If the pool holds fewer bytes than `target_blocks × max_block_bytes`, the loop runs to completion over every entry.

The call chain from the public RPC surface is:

1. `rpc/src/module/experiment.rs` → `estimate_fee_rate` RPC handler
2. → `TxPoolController::estimate_fee_rate`
3. → `TxPoolService::estimate_fee_rate` (fallback branch when primary estimator returns `Err`)
4. → `TxPool::estimate_fee_rate` (validates `target_to_be_committed ∈ [3, 131]`)
5. → `PoolMap::estimate_fee_rate` — the unbounded loop [2](#0-1) [3](#0-2) 

The fallback is reached whenever the primary fee estimator (`ConfirmationFraction` / `WeightUnitsFlow`) returns an error — a routine condition during initial sync or after a node restart — and `enable_fallback` is `true` (the default).

The pool size limit is enforced in bytes, not in transaction count:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size { ... }
``` [4](#0-3) 

With `max_tx_pool_size = 180_000_000` bytes (default) and a minimum serialised transaction size of roughly 170–200 bytes, the pool can hold approximately **900 000–1 000 000 entries**. When the pool is filled with such transactions but their aggregate size is below `target_blocks × max_block_bytes` (~77 MB for `target_blocks = 129`), every call to `estimate_fee_rate` scans the entire pool.

### Impact Explanation
- **CPU exhaustion**: iterating ~900 K entries per RPC call with arithmetic on each entry consumes significant CPU time per invocation.
- **Write-lock starvation**: the async read-lock on `tx_pool` is held for the duration of the scan. Concurrent writers (transaction submission, block attachment, pool eviction) must wait, delaying transaction processing and block assembly.
- **Amplification**: the attacker can issue `estimate_fee_rate` calls in a tight loop; each call is cheap for the caller but expensive for the node.

### Likelihood Explanation
- **Pool filling**: any remote peer can relay minimum-fee transactions via the relay protocol. Filling 50 MB of the pool with ~300 K minimum-size transactions costs roughly 0.5–1 CKB in fees — a low barrier.
- **RPC trigger**: `estimate_fee_rate` is a standard, unauthenticated RPC endpoint accessible to any local or remotely-configured RPC caller. No privileged role is required.
- **Fallback activation**: the fallback path is active by default and is routinely triggered on freshly started or recently synced nodes, making the window of exposure wide.

### Recommendation
1. **Cap the iteration**: add a hard upper bound on the number of entries scanned per call (e.g., `iter.take(max_scan_entries)`) so that a single RPC call cannot scan the entire pool.
2. **Count-based pool limit**: enforce a maximum transaction count in addition to the byte-size limit, directly bounding the worst-case iteration depth.
3. **Rate-limit the RPC**: apply per-caller rate limiting on `estimate_fee_rate` to prevent rapid repeated invocations.

### Proof of Concept
1. Connect to a CKB node whose primary fee estimator is not yet ready (e.g., immediately after startup).
2. Relay ~300 000 minimum-size, minimum-fee transactions via the P2P relay protocol to fill ~50 MB of the pool (below the 77 MB threshold needed to fill 129 blocks).
3. In a tight loop, call `estimate_fee_rate` with default parameters (`estimate_mode = null`, `enable_fallback = true`).
4. Observe that each call triggers `PoolMap::estimate_fee_rate`, which scans all ~300 000 entries; sustained CPU usage rises and concurrent `submit_transaction` / block-assembly latency increases measurably.

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

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
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
