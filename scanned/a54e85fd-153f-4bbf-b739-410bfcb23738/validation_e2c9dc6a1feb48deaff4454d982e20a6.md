### Title
`estimate_fee_rate` Fallback Uses Spot Tx-Pool State Manipulable by Any Tx Submitter — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

The `estimate_fee_rate` fallback algorithm in CKB's tx-pool reads the **current (spot) state** of the mempool to produce a fee-rate recommendation. Any unprivileged transaction submitter can flood the pool with crafted transactions to skew this estimate arbitrarily upward or downward, misleading wallets and users that rely on the RPC into overpaying or having their transactions permanently stuck.

---

### Finding Description

The fallback path of `estimate_fee_rate` is invoked whenever the primary statistical estimator (`ConfirmationFraction` or `WeightUnitsFlow`) lacks sufficient historical data (e.g., shortly after node startup or after IBD). It is exposed via the public `experiment_estimate_fee_rate` JSON-RPC method.

The fallback is implemented in `tx-pool/src/component/pool_map.rs`:

```rust
pub(crate) fn estimate_fee_rate(
    &self,
    mut target_blocks: usize,
    max_block_bytes: usize,
    max_block_cycles: Cycle,
    min_fee_rate: FeeRate,
) -> FeeRate {
    let iter = self.entries.iter_by_score().rev();
    let mut current_block_bytes = 0;
    let mut current_block_cycles = 0;
    for entry in iter {
        current_block_bytes += entry.inner.size;
        current_block_cycles += entry.inner.cycles;
        if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
            target_blocks -= 1;
            if target_blocks == 0 {
                return entry.inner.fee_rate();   // ← spot reading
            }
            ...
        }
    }
    min_fee_rate
}
```

The function iterates `self.entries` — the **live, mutable pool** — sorted by ancestor-score and simulates how many simulated blocks it takes to drain the pool. The fee rate of the last entry that fills the `target_blocks`-th simulated block is returned as the recommendation. There is no smoothing, no time-averaging, and no protection against pool-state manipulation.

The call chain is:

1. `ExperimentRpc::estimate_fee_rate` (`rpc/src/module/experiment.rs:301–315`) calls
2. `TxPoolController::estimate_fee_rate` → `TxPoolService::estimate_fee_rate` (`tx-pool/src/process.rs:945–970`) which, on `Error::LackData`, falls back to
3. `TxPool::estimate_fee_rate` (`tx-pool/src/pool.rs:557–572`) which calls
4. `PoolMap::estimate_fee_rate` (`tx-pool/src/component/pool_map.rs:334–359`) — the spot read.

The `WeightUnitsFlow` primary estimator does maintain a rolling historical window (`self.txs` keyed by block number, expired after `historical_blocks`), but the fallback bypasses all of that and reads only the instantaneous pool state.

---

### Impact Explanation

An attacker who controls one or more transaction-submitting identities can:

**Inflate the estimate (fee-rate pump):** Submit a large number of transactions that collectively fill several simulated blocks, each paying a high fee rate. The fallback will report that fee rate as the required rate. Wallets and users that call `estimate_fee_rate` will overpay by an arbitrary multiple, transferring excess fees to miners (or to the attacker if they are also mining).

**Deflate the estimate (fee-rate suppression):** Submit many minimum-fee-rate transactions that fill the pool. The fallback will report `min_fee_rate` as sufficient. Legitimate users who follow this advice will have their transactions stuck behind the attacker's flood, effectively a targeted mempool denial-of-service.

Because the pool is a shared, globally-visible resource and the RPC is unauthenticated, any node that exposes the `Experiment` RPC module is affected.

---

### Likelihood Explanation

- The `estimate_fee_rate` RPC is publicly documented and enabled by default in the `Experiment` module.
- Submitting transactions to the pool requires only meeting `min_fee_rate`; no privileged access is needed.
- The attack is cheap: the attacker's transactions will eventually be mined or expire (`expiry_hours`), so the cost is bounded by the minimum fee paid on the flood transactions.
- Wallets and tooling that call `estimate_fee_rate` during the node's warm-up period (before the primary estimator has enough data) are the most exposed, but the fallback can also be triggered at any time by calling with `enable_fallback: true`.

---

### Recommendation

1. **Do not use the live pool as the sole input to fee estimation.** The fallback should use a time-windowed sample of recently-confirmed transactions (as `ConfirmationFraction` does) rather than the instantaneous pool state.
2. **Apply outlier filtering or percentile capping** before returning a fee-rate recommendation, so that a sudden spike in pool contents does not immediately propagate to the estimate.
3. **Rate-limit or authenticate** the `estimate_fee_rate` RPC if the node operator does not want it exposed to arbitrary callers.
4. **Document clearly** that the fallback result is based on the current pool snapshot and may be manipulated, so that callers can apply their own sanity bounds.

---

### Proof of Concept

```
1. Node starts (or IBD just finished); ConfirmationFraction/WeightUnitsFlow
   has < historical_blocks of data → LackData error → fallback is used.

2. Attacker submits N transactions, each ~max_block_bytes/N bytes,
   each paying fee_rate = 10× the honest market rate.
   These fill target_blocks simulated blocks in the fallback loop.

3. Honest wallet calls:
     POST /rpc  {"method":"estimate_fee_rate","params":[]}
   Response: 10× the real market rate.

4. Wallet constructs and submits a transaction paying 10× the necessary fee.
   Excess fee goes to the miner; user loses funds.

5. Attacker's flood transactions expire (expiry_hours) or get mined;
   attack cost = N × min_fee_rate × tx_size.
```

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
