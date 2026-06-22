### Title
Manipulable Spot Fee Rate Used as Fallback When Primary Estimator Lacks Data — (`tx-pool/src/component/pool_map.rs`, `tx-pool/src/process.rs`)

---

### Summary

The `estimate_fee_rate` RPC falls back to reading the current instantaneous pool state (a "spot" fee rate) whenever the primary fee estimator lacks sufficient historical data. Because any unprivileged tx-pool submitter can flood the pool with artificially high-fee transactions, the fallback value is fully attacker-controlled. Users who call `estimate_fee_rate` with the default `enable_fallback = true` during the fallback window will receive an inflated fee rate and overpay.

---

### Finding Description

**Fallback trigger — `tx-pool/src/process.rs`** [1](#0-0) 

When `self.fee_estimator.estimate_fee_rate(...)` returns any error (`Error::NotReady`, `Error::LackData`, `Error::Dummy`, `Error::NoProperFeeRate`), the code immediately falls back to `self.tx_pool.read().await.estimate_fee_rate(target_blocks)`, which reads the live pool state at that instant.

The primary estimator returns errors in common, reachable conditions:
- Node just started or just exited IBD — `is_ready` is `false`, returning `Error::NotReady`.
- Insufficient historical block data — `WeightUnitsFlow` returns `Error::LackData`.
- Estimator configured as `Dummy` — always returns `Error::Dummy`. [2](#0-1) [3](#0-2) 

**Spot fee rate computation — `tx-pool/src/component/pool_map.rs`** [4](#0-3) 

The fallback iterates pool entries sorted by score in descending order and returns the fee rate of the entry sitting at the target-block boundary. This is a pure snapshot of the current pool — no time-averaging, no historical smoothing.

**Default `enable_fallback = true` in the RPC** [5](#0-4) 

Unless the caller explicitly passes `enable_fallback: false`, the fallback is always active.

---

### Impact Explanation

An attacker submits a batch of transactions with artificially high fee rates into the pool. When the primary estimator is not ready (e.g., immediately after IBD exit, or on a freshly started node), the fallback reads the current pool state and returns the attacker-inflated fee rate. Wallets and users that call `estimate_fee_rate` with the default parameters receive this manipulated value and set their own transaction fees accordingly, causing them to overpay. The attacker can time the manipulation to coincide with high-value user activity (e.g., a token launch) to maximize the economic harm.

---

### Likelihood Explanation

- The fallback window is **common**: every node that exits IBD or restarts clears its historical estimator state, making the fallback the only active path until enough blocks accumulate.
- The attacker entry path requires **no privilege**: any peer can submit transactions to the tx-pool via the P2P relay or the `send_transaction` RPC.
- The cost to the attacker is bounded by the fees paid on the injected transactions, which are eventually mined and returned as miner rewards — the attacker can be the miner themselves, making the net cost near zero.
- The `enable_fallback` parameter defaults to `true`, so the vast majority of real-world callers are affected without any special configuration.

---

### Recommendation

1. **Do not use the live pool snapshot as a fallback.** When the primary estimator is not ready, return an explicit error rather than silently falling back to a manipulable spot value. Callers can then decide to use a hardcoded safe minimum or display a "fee estimation unavailable" message.
2. **If a fallback is required**, use a time-weighted or percentile-based value derived from recently committed blocks (which are immutable and not attacker-injectable in the same window), rather than the current pending pool.
3. **Document the manipulation risk** in the RPC description so that wallet developers know not to blindly trust the fallback value during the post-IBD warm-up period.

---

### Proof of Concept

1. Wait for or trigger the fallback condition: restart the node or let it exit IBD. The `WeightUnitsFlow` or `ConfirmationFraction` estimator will return `Error::NotReady` or `Error::LackData`.
2. Submit N transactions with fee rates far above the market rate via `send_transaction` RPC or P2P relay. These land in the pending pool and are sorted by score.
3. Call `estimate_fee_rate` (with default `enable_fallback: null`). The code path in `process.rs:957–964` triggers `pool_map.estimate_fee_rate`, which iterates the score-sorted pool and returns the fee rate at the target-block boundary — now set by the attacker's high-fee transactions.
4. Any wallet that uses this returned value to set its own fee will overpay by the attacker-chosen margin. [6](#0-5) [7](#0-6)

### Citations

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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L169-171)
```rust
        if !self.is_ready {
            return Err(Error::NotReady);
        }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L479-484)
```rust
    pub fn estimate_fee_rate(&self, target_blocks: BlockNumber) -> Result<FeeRate, Error> {
        if !self.is_ready {
            return Err(Error::NotReady);
        }
        self.estimate(target_blocks)
    }
```

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

**File:** rpc/src/module/experiment.rs (L306-314)
```rust
        let estimate_mode = estimate_mode.unwrap_or_default();
        let enable_fallback = enable_fallback.unwrap_or(true);
        self.shared
            .tx_pool_controller()
            .estimate_fee_rate(estimate_mode.into(), enable_fallback)
            .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?
            .map_err(RPCError::from_any_error)
            .map(core::FeeRate::as_u64)
            .map(Into::into)
```
