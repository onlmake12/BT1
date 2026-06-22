### Title
Tx-Pool Spot-State Fee Estimation Is Manipulable by Unprivileged Transaction Senders — (`tx-pool/src/component/pool_map.rs`, `util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

Both the fallback fee estimator (`PoolMap::estimate_fee_rate`) and the primary `WeightUnitsFlow` estimator derive their output exclusively from the **current live state of the tx-pool**, which any unprivileged transaction sender can freely manipulate. This is the direct CKB analog of using `calc_withdraw_one_coin` (a Curve AMM spot price) for collateral valuation: a mutable, attacker-influenced pool snapshot is used as a trusted oracle for a financial decision (fee rate), with no manipulation resistance.

---

### Finding Description

**Fallback algorithm — `PoolMap::estimate_fee_rate`**

`PoolMap::estimate_fee_rate` iterates over all pool entries sorted by fee-rate score and simulates filling blocks to find the minimum fee rate needed to be included within `target_blocks` blocks.

```rust
// tx-pool/src/component/pool_map.rs:334-359
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
                return entry.inner.fee_rate();   // ← spot price from live pool
            }
            ...
        }
    }
    min_fee_rate
}
```

The returned value is the fee rate of whichever entry happens to sit at the simulated block boundary at the moment of the call. [1](#0-0) 

**Primary algorithm — `WeightUnitsFlow::estimate_fee_rate`**

The primary estimator reads `all_entry_info` (the full live pending + proposed pool snapshot) and builds `current_weight_buckets` from it. The highest fee-rate transaction in the pool at call time sets `max_fee_rate`, which determines the number of buckets and therefore the entire bucket structure used for the estimate.

```rust
// util/fee-estimator/src/estimator/weight_units_flow.rs:173-184
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
``` [2](#0-1) 

Inside `do_estimate`, `max_fee_rate` is taken directly from the highest-fee-rate entry in the live pool:

```rust
// util/fee-estimator/src/estimator/weight_units_flow.rs:205-210
let max_fee_rate = if let Some(fee_rate) = sorted_current_txs.first().map(|tx| tx.fee_rate)
{
    fee_rate
} else {
    return Ok(constants::LOWEST_FEE_RATE);
};
``` [3](#0-2) 

This value is then used to size the bucket array and compute `current_weight_buckets`, which drives the final fee rate output. [4](#0-3) 

**RPC exposure**

Both paths are reachable via the public `estimate_fee_rate` JSON-RPC endpoint in `rpc/src/module/experiment.rs`, which is callable by any unprivileged RPC user. [5](#0-4) 

The process handler reads the pool under a read-lock and passes the snapshot directly to the estimator: [6](#0-5) 

---

### Impact Explanation

An attacker who can submit transactions to the pool (any unprivileged tx-pool submitter or RPC caller) can:

1. **Inflate the estimate (upward manipulation):** Submit many large, high-fee-rate transactions. The fallback algorithm's block-fill simulation will hit the block byte/cycle limit at a high-fee-rate entry, returning an inflated fee rate. The `WeightUnitsFlow` algorithm will create high-index buckets with large `current_weight`, causing the estimate to return a high fee rate. Victims who rely on the estimate overpay fees.

2. **Deflate the estimate (downward manipulation):** Flood the pool with many minimum-fee-rate transactions. The fallback algorithm will simulate blocks filled with cheap transactions and return `min_fee_rate`. Victims who rely on the estimate underpay and have their transactions stuck or delayed.

In both cases, the attacker controls the "spot price" (pool state) used for the financial decision (fee rate), directly analogous to manipulating `calc_withdraw_one_coin` to control the collateral valuation.

---

### Likelihood Explanation

- **Entry path is fully open:** Any node that accepts inbound transactions (via P2P relay or `send_transaction` RPC) is reachable by an unprivileged attacker.
- **Cost is bounded by `min_fee_rate`:** The attacker must pay at least `min_fee_rate` per byte for admitted transactions. However, the attacker can reclaim funds by spending their own UTXOs in the manipulation transactions, and the cost per manipulation event is low relative to the harm caused to many victims.
- **No rate limiting on `estimate_fee_rate` RPC:** Victims can be continuously fed manipulated estimates.
- **Likelihood: Medium.** The attack requires on-chain funds and transaction submission, but no privileged access, no majority hashpower, and no social engineering.

---

### Recommendation

- **Short term:** Do not use the live pool snapshot as the sole input to fee estimation. Apply a time-weighted or block-confirmed moving average over recent pool states rather than the instantaneous snapshot.
- **Long term:** Mirror the recommendation from the original report: use a manipulation-resistant oracle. For CKB, this means basing fee estimates primarily on **confirmed historical data** (committed blocks and their fee distributions), not on the current unconfirmed pool state. The `ConfirmationFraction` and `WeightUnitsFlow` historical components already track committed-block data; the vulnerability is in the `current_weight_buckets` component that mixes in live pool state without any manipulation guard.

---

### Proof of Concept

1. Attacker holds a UTXO with sufficient capacity.
2. Attacker calls `send_transaction` RPC repeatedly, submitting N transactions spending their own cells, each with a fee rate of `X` shannons/kB (where `X` >> `min_fee_rate`).
3. Victim calls `estimate_fee_rate` RPC with `estimate_mode: "no_priority"` and `enable_fallback: true`.
4. **Fallback path:** `PoolMap::estimate_fee_rate` iterates entries by score (descending). The attacker's high-fee entries fill simulated blocks first. The function returns the fee rate of the entry at the `target_blocks`-th block boundary — the attacker's inflated fee rate.
5. **Primary path (`WeightUnitsFlow`):** `sorted_current_txs.first()` is the attacker's highest-fee entry. `max_fee_rate` is set to the attacker's fee rate. `current_weight_buckets` at the attacker's bucket index is large. `do_estimate` finds that the attacker's bucket does not pass (`current_weight + added_weight > removed_weight`) and returns a fee rate at or above the attacker's fee rate.
6. Victim uses the returned fee rate to construct their transaction and overpays.
7. Attacker can RBF their manipulation transactions out of the pool (paying only the RBF delta) or let them expire, recovering most of their capital.

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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L205-210)
```rust
        let max_fee_rate = if let Some(fee_rate) = sorted_current_txs.first().map(|tx| tx.fee_rate)
        {
            fee_rate
        } else {
            return Ok(constants::LOWEST_FEE_RATE);
        };
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L218-237)
```rust
        let current_weight_buckets = {
            let mut buckets = vec![0u64; max_bucket_index + 1];
            let mut index_curr = max_bucket_index;
            for tx in sorted_current_txs {
                let index = Self::max_bucket_index_by_fee_rate(tx.fee_rate);
                if index < index_curr {
                    let weight_curr = buckets[index_curr];
                    for i in buckets.iter_mut().take(index_curr) {
                        *i = weight_curr;
                    }
                }
                buckets[index] += tx.weight;
                index_curr = index;
            }
            let weight_curr = buckets[index_curr];
            for i in buckets.iter_mut().take(index_curr) {
                *i = weight_curr;
            }
            buckets
        };
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
