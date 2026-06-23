### Title
Live Mempool State Used for Fee Rate Estimation Can Be Manipulated by Unprivileged Transaction Submitters — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

The `estimate_fee_rate` RPC (Experiment module) calculates its result by directly iterating over the live mempool state. Because any unprivileged peer can inject transactions into the pool via `send_transaction`, an attacker can flood the pool with high-fee-rate transactions to artificially inflate the returned fee estimate. Victims who rely on this estimate to set their own transaction fees will systematically overpay.

---

### Finding Description

The fallback path of `estimate_fee_rate` is implemented in `PoolMap::estimate_fee_rate` (`tx-pool/src/component/pool_map.rs`). It iterates over every pool entry sorted by fee-rate score in descending order, simulating block-filling until `target_blocks` virtual blocks are consumed, then returns the fee rate of the boundary entry: [1](#0-0) 

This function is called from `TxPool::estimate_fee_rate` in `tx-pool/src/pool.rs`, which feeds it the consensus block-byte and block-cycle limits directly from the current snapshot: [2](#0-1) 

The primary `WeightUnitsFlow` algorithm also reads live pool state. In `util/fee-estimator/src/estimator/weight_units_flow.rs`, `estimate_fee_rate` accepts `all_entry_info` — the full set of current pending and proposed transactions — and builds weight buckets from it before computing the estimate: [3](#0-2) 

Both paths are triggered by the same RPC handler in `rpc/src/module/experiment.rs`, which is reachable by any RPC caller without authentication: [4](#0-3) 

The full call chain is:

```
RPC caller → ExperimentRpcImpl::estimate_fee_rate
           → TxPoolController::estimate_fee_rate (service.rs)
           → TxPoolService::estimate_fee_rate (process.rs, L945-970)
               ├─ FeeEstimator::estimate_fee_rate (WeightUnitsFlow, uses all_entry_info)
               └─ [fallback] TxPool::estimate_fee_rate (pool.rs, L557-572)
                             └─ PoolMap::estimate_fee_rate (pool_map.rs, L334-359)
```

The documentation for the fallback algorithm explicitly acknowledges its assumptions — that all pool transactions are waiting to be proposed and no new ones will arrive — but provides no protection against an adversary who deliberately violates those assumptions: [5](#0-4) 

---

### Impact Explanation

An attacker who submits a large volume of valid, high-fee-rate transactions into the pool causes `PoolMap::estimate_fee_rate` to encounter those entries first (they sort to the top of `iter_by_score().rev()`). The virtual block boundary is reached at a much higher fee rate than the true market rate, so the function returns an inflated value. Any wallet, dApp, or automated system that calls `estimate_fee_rate` and uses the result to set its own transaction fee will overpay — potentially by a large multiple of the real market rate. The `WeightUnitsFlow` path is equally affected because `current_weight_buckets` is built from the same attacker-controlled pool entries, shifting the bucket threshold upward.

---

### Likelihood Explanation

The attack requires only that the attacker hold valid live UTXOs and submit transactions above `min_fee_rate`. No privileged access, key material, or majority hash power is needed. The attacker can time the injection to coincide with a victim's fee-estimation call, then allow their own transactions to expire (default `expiry_hours = 12`) or be evicted when the pool fills. The `send_transaction` RPC is open to any network peer, making this straightforwardly reachable. The cost to the attacker is the fees paid on their injected transactions, but those transactions need never be confirmed — they only need to occupy the pool long enough for the victim to query the estimate.

---

### Recommendation

Replace the live-pool snapshot used for fee estimation with a time-decayed historical sample that is not directly writable by transaction submitters, analogous to how `ConfirmationFraction` tracks confirmed-block data rather than current pool contents. At minimum, cap the influence of any single fee-rate bucket on the estimate, and document clearly that `estimate_fee_rate` results should not be used without an independent sanity bound (e.g., a hard ceiling or a comparison against a recent confirmed-block median).

---

### Proof of Concept

1. Observe the baseline: call `estimate_fee_rate` on a node with a lightly loaded pool. Record the result (e.g., 1 000 shannons/KB).
2. Using a set of UTXOs, submit a large batch of transactions via `send_transaction` each paying a fee rate of, say, 500 000 shannons/KB — well above the market rate but valid per `min_fee_rate`.
3. Immediately call `estimate_fee_rate` again. `PoolMap::estimate_fee_rate` iterates the pool in descending fee-rate order; the attacker's entries fill the first virtual block, so the boundary entry has fee rate ≈ 500 000 shannons/KB. The RPC returns this inflated value.
4. A victim wallet that called `estimate_fee_rate` between steps 2 and 3 sets its fee accordingly and overpays by ~500×.
5. After `expiry_hours` (default 12 h), the attacker's transactions expire from the pool with no confirmation required, ending the attack at the cost of only the fees on the injected transactions (which were never mined).

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

**File:** rpc/src/module/experiment.rs (L179-187)
```rust
    /// ### The fallback algorithm
    ///
    /// Since CKB transaction confirmation involves a two-step process—1) propose and 2) commit, it is complex to
    /// predict the transaction fee accurately with the expectation that it will be included within a certain block height.
    ///
    /// This algorithm relies on two assumptions and uses a simple strategy to estimate the transaction fee: 1) all transactions
    /// in the pool are waiting to be proposed, and 2) no new transactions will be added to the pool.
    ///
    /// In practice, this simple algorithm should achieve good accuracy fee rate and running performance.
```

**File:** rpc/src/module/experiment.rs (L301-315)
```rust
    fn estimate_fee_rate(
        &self,
        estimate_mode: Option<EstimateMode>,
        enable_fallback: Option<bool>,
    ) -> Result<Uint64> {
        let estimate_mode = estimate_mode.unwrap_or_default();
        let enable_fallback = enable_fallback.unwrap_or(true);
        self.shared
            .tx_pool_controller()
            .estimate_fee_rate(estimate_mode.into(), enable_fallback)
            .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?
            .map_err(RPCError::from_any_error)
            .map(core::FeeRate::as_u64)
            .map(Into::into)
    }
```
