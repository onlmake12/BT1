### Title
Fee Rate Estimate Manipulation via Single-Source Tx-Pool State — (File: `util/fee-estimator/src/estimator/weight_units_flow.rs`, `tx-pool/src/component/pool_map.rs`)

### Summary
The CKB fee rate estimators derive their output from a single, attacker-manipulable source — the live tx-pool state — with no historical smoothing, multi-source averaging, or TWAP-equivalent dampening. An unprivileged tx-pool submitter can flood the pool with transactions at chosen fee rates to inflate or deflate the estimate returned via the `estimate_fee_rate` RPC, causing honest users to systematically overpay fees or have their transactions stuck.

### Finding Description

CKB exposes two fee rate estimators. Both share the same root cause.

**1. `WeightUnitsFlow::estimate_fee_rate`** (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

The function takes `all_entry_info` — a live snapshot of every pending and proposed transaction in the pool — and builds `current_weight_buckets` entirely from that snapshot: [1](#0-0) 

The `flow_speed_buckets` are built from `self.txs`, which records every transaction accepted into the pool since `boot_tip`: [2](#0-1) 

Both inputs are fully attacker-controlled: any tx-pool submitter can insert transactions at arbitrary fee rates. There is no dampening, no multi-block median, and no minimum sample requirement that would resist a sudden flood.

**2. Fallback `pool_map::estimate_fee_rate`** (`tx-pool/src/component/pool_map.rs`)

This fallback (used when the primary estimator returns `Error::LackData` or `Error::NotReady`) is even more exposed: it iterates the pool sorted by score and returns the fee rate of the entry at the `target_blocks` boundary — a pure instantaneous snapshot with zero historical context: [3](#0-2) 

The fallback is invoked from `tx-pool/src/process.rs`: [4](#0-3) 

The result is surfaced to any RPC caller via `estimate_fee_rate` in `rpc/src/module/experiment.rs`.

**Contrast with `get_fee_rate_statistics`**

The separate `get_fee_rate_statistics` RPC (`rpc/src/util/fee_rate.rs`) collects fee rates from *confirmed* blocks over a configurable window (default 21 blocks), which is far more resistant to manipulation. The fee estimators do not use this historical data. [5](#0-4) 

### Impact Explanation

An attacker who controls a set of UTXOs can:

1. **Inflate the estimate (overpayment attack)**: Submit a large volume of transactions with artificially high fee rates. Wallets and dApps querying `estimate_fee_rate` receive an inflated value and instruct their users to pay excessive fees. The attacker's transactions are eventually mined or expire; the victim has already committed to the inflated fee.

2. **Deflate the estimate (stuck-transaction attack)**: Flood the pool with minimum-fee-rate transactions (just above `min_fee_rate`, which is a static config value). The estimator returns a low fee rate. Victims submit transactions at that rate; when the attacker's flood clears, the real market rate is higher and victims' transactions are stuck or evicted.

The `min_fee_rate` check in `tx-pool/src/util.rs` uses a static config value, not the estimator output, so tx admission is unaffected — but the estimate consumed by external clients is corrupted. [6](#0-5) 

### Likelihood Explanation

The entry path is fully open to any unprivileged tx-pool submitter or RPC caller. No mining power, no privileged key, and no Sybil capability is required. The attacker only needs enough CKB to pay the minimum fee for a batch of transactions. The `WeightUnitsFlow` estimator's `flow_speed_buckets` can be poisoned gradually over `historical_blocks = target_blocks * 2` blocks, making the attack persistent and cheap to sustain.

### Recommendation

1. **Use confirmed-block data as the primary source.** The existing `FeeRateCollector` in `rpc/src/util/fee_rate.rs` already computes a median over confirmed blocks. The fee estimators should incorporate or cross-check against this historical signal.
2. **Apply a dampening / TWAP-equivalent filter** on the pool-state snapshot before using it in `do_estimate`, analogous to the `bounding_hash_rate` dampening already applied in the difficulty adjustment (`spec/src/consensus.rs`).
3. **Require a minimum number of confirmed-block samples** before the pool-state estimators are considered ready, reducing the window during which a fresh-pool state can be fully attacker-controlled.
4. **Rate-limit or bound the influence of a single block's worth of pool entries** on the `flow_speed_buckets` calculation.

### Proof of Concept

```
1. Attacker holds UTXOs sufficient to create N transactions.
2. Attacker submits N transactions each with fee_rate = R_high
   (e.g., 2_000_000 shannons/KW, bucket index ~137 in weight_units_flow.rs).
3. Victim calls estimate_fee_rate(HighPriority) via RPC.
4. WeightUnitsFlow::do_estimate sees current_weight_buckets dominated by
   attacker's transactions at bucket 137; flow_speed_buckets also elevated.
5. The cheapest bucket whose final_weight <= removed_weight is now bucket 137,
   so the RPC returns ~2_000_000 shannons/KW instead of the true market rate.
6. Victim's wallet submits a transaction at 2_000_000 shannons/KW, overpaying
   by orders of magnitude.
7. Attacker's transactions are mined in the next block (they have the highest
   fee rate) or expire; the pool returns to normal.
```

The fallback path (`pool_map::estimate_fee_rate`) is even simpler to exploit: it requires only that the primary estimator is not yet ready (e.g., node just started), which is a common condition during IBD exit. [7](#0-6) [8](#0-7)

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L153-162)
```rust
    pub fn accept_tx(&mut self, info: TxEntryInfo) {
        if self.current_tip == 0 {
            return;
        }
        let item = TxStatus::new_from_entry_info(info);
        self.txs
            .entry(self.current_tip)
            .and_modify(|items| items.push(item))
            .or_insert_with(|| vec![item]);
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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L244-298)
```rust
        // Calculate flow speeds for buckets.
        let flow_speed_buckets = {
            let historical_tip = self.current_tip - historical_blocks;
            let sorted_flowed = self.sorted_flowed(historical_tip);
            let mut buckets = vec![0u64; max_bucket_index + 1];
            let mut index_curr = max_bucket_index;
            for tx in &sorted_flowed {
                let index = Self::max_bucket_index_by_fee_rate(tx.fee_rate);
                if index > max_bucket_index {
                    continue;
                }
                if index < index_curr {
                    let flowed_curr = buckets[index_curr];
                    for i in buckets.iter_mut().take(index_curr) {
                        *i = flowed_curr;
                    }
                }
                buckets[index] += tx.weight;
                index_curr = index;
            }
            let flowed_curr = buckets[index_curr];
            for i in buckets.iter_mut().take(index_curr) {
                *i = flowed_curr;
            }
            buckets
                .into_iter()
                .map(|value| value / historical_blocks)
                .collect::<Vec<_>>()
        };
        for (index, speed) in flow_speed_buckets.iter().enumerate() {
            if *speed != 0 {
                ckb_logger::trace!(">>> flow_speed[{index}]: {speed}");
            }
        }

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

**File:** tx-pool/src/process.rs (L945-969)
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
```

**File:** rpc/src/util/fee_rate.rs (L79-121)
```rust
    pub fn statistics(&self, target: Option<u64>) -> Option<FeeRateStatistics> {
        let mut target = target.unwrap_or(DEFAULT_TARGET);
        if is_even(target) {
            target = target.saturating_add(1);
        }
        target = std::cmp::min(self.provider.max_target(), target);

        let mut fee_rates = self.provider.collect(target, |mut fee_rates, block_ext| {
            let BlockExt {
                txs_sizes,
                cycles,
                txs_fees,
                ..
            } = block_ext;
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
            if txs_sizes.len() > 1 && !txs_fees.is_empty() {
                // block_ext.txs_fees's length == block_ext.cycles's length
                // block_ext.txs_fees's length + 1 == txs_sizes's length
                if let Some(cycles) = cycles {
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
                    }
                }
            }
            fee_rates
        });

        if fee_rates.is_empty() {
            None
        } else {
            Some(FeeRateStatistics {
                mean: mean(&fee_rates).into(),
                median: median(&mut fee_rates).into(),
            })
        }
    }
```

**File:** tx-pool/src/util.rs (L44-52)
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
```
