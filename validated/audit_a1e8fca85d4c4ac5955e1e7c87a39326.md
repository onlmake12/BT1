### Title
Fee Rate Oracle Manipulation via Persistent Historical Flow Poisoning in WeightUnitsFlow Estimator - (File: `util/fee-estimator/src/estimator/weight_units_flow.rs`)

### Summary

The `WeightUnitsFlow` fee estimator's `do_estimate` function builds its fee rate estimate from two inputs: the current mempool state (`current_weight_buckets`) and a historical transaction flow record (`flow_speed_buckets`). The historical flow store (`self.txs`) accumulates every transaction that enters the pool keyed by block number, but **never removes entries when transactions leave the pool** (confirmed, evicted, or expired). An unprivileged tx-pool submitter can poison this historical record by submitting a burst of transactions with artificially high fee rates, causing the estimator to return an inflated fee rate for up to `historical_blocks * 2` (~256) subsequent blocks — long after the attacker's transactions are gone.

### Finding Description

**Root cause — `accept_tx` writes to `self.txs` but no corresponding removal exists:** [1](#0-0) 

Every transaction that enters the pool is appended to `self.txs[current_tip]`. When a transaction is confirmed, evicted, or expires from the pool, the `WeightUnitsFlow` algorithm has **no `reject_tx` / `drop_tx` path** — compare with `ConfirmationFraction` which does: [2](#0-1) 

The `WeightUnitsFlow` variant is silently ignored on rejection. Entries in `self.txs` are only pruned by the `expire()` call on each new block, which retains data for `historical_blocks(MAX_TARGET) = 256` blocks: [3](#0-2) 

**How `do_estimate` uses the poisoned data:**

`flow_speed_buckets` is built from `self.txs` filtered to the last `historical_blocks` window: [4](#0-3) 

The `added_weight = flow_speed_buckets[bucket_index] * target_blocks` term is then added to `current_weight` in the pass/fail check: [5](#0-4) 

If `flow_speed_buckets` is inflated for high-fee-rate buckets, the algorithm cannot find a low-fee bucket that satisfies `current_weight + added_weight <= removed_weight`, so it returns a higher fee rate estimate (or `Error::NoProperFeeRate`, triggering the fallback).

**`max_bucket_index` is also attacker-controlled:**

The bucket array size is determined dynamically by the highest fee rate currently in the mempool: [6](#0-5) 

A single transaction with an extremely high fee rate (e.g., paying 10,000 CKB fee on a minimal-weight transaction) causes `max_bucket_index_by_fee_rate` to return a very large index, allocating proportionally large bucket arrays on every `estimate_fee_rate` call. For fee rates in the range achievable by a well-funded attacker, this can reach hundreds of megabytes per call. [7](#0-6) 

**Entry path — unprivileged RPC caller:**

Any node operator or user can call `send_transaction` to inject transactions into the pool. The `register_pending` callback unconditionally calls `fee_estimator.accept_tx`: [8](#0-7) 

The `estimate_fee_rate` RPC is publicly accessible: [9](#0-8) 

### Impact Explanation

1. **Inflated fee rate oracle output**: After the attacker's transactions are confirmed or evicted, `self.txs` retains their high-fee-rate entries for up to 256 blocks. During this window, every call to `estimate_fee_rate` (WeightUnitsFlow mode) returns an inflated estimate. Wallets and dApps that rely on this RPC will overpay transaction fees.

2. **Loss of estimator resilience**: The estimator is supposed to reflect the organic fee market. The persistent historical store means a brief burst of attacker-controlled transactions dominates the `flow_speed_buckets` for an extended period — analogous to the oracle in the external report being forced to rely on a single manipulated pool.

3. **Potential memory pressure**: A transaction with a sufficiently high fee rate causes `max_bucket_index` to be very large, triggering large heap allocations on every `estimate_fee_rate` call while that transaction remains in the pool.

### Likelihood Explanation

- Entry path requires no privilege — any `send_transaction` caller qualifies.
- The attacker must pay fees (unlike a flash loan), but the poisoning effect persists for ~256 blocks (~2 hours), giving a high amplification ratio relative to cost.
- The `WeightUnitsFlow` estimator is the default for nodes that configure it; the fallback (`TxPool::estimate_fee_rate`) is simpler and less affected, but the primary estimator is the target.

### Recommendation

1. **Remove historical entries when transactions leave the pool**: Add a `reject_tx` / `drop_tx` path to `WeightUnitsFlow::Algorithm` (mirroring `ConfirmationFraction`) that removes the transaction's weight contribution from `self.txs`.

2. **Cap `max_bucket_index`**: Clamp `max_bucket_index_by_fee_rate` to a fixed maximum (e.g., 200, matching the `ConfirmationFraction` bucket count) to bound memory allocation regardless of attacker-controlled fee rates.

3. **Apply a decay or cap on `flow_speed_buckets`**: Weight historical flow contributions by recency, or cap the per-block contribution, so a single burst cannot dominate the estimate for the full 256-block window.

### Proof of Concept

**Step 1 — Attacker submits N transactions with fee_rate = R (very high):**
```
send_transaction(tx_i)  for i in 1..N
```
Each call triggers `accept_tx` → `self.txs[current_tip].push(TxStatus { weight, fee_rate: R })`.

**Step 2 — Transactions are confirmed or evicted (next block):**
`self.txs` still contains all N entries at `current_tip`. No removal occurs.

**Step 3 — Victim calls `estimate_fee_rate` for the next 256 blocks:**
`sorted_flowed(historical_tip)` returns all N attacker entries. `flow_speed_buckets[high_bucket] = N * weight / historical_blocks`. For all lower buckets, `added_weight = flow_speed_buckets[bucket] * target_blocks` is inflated. The algorithm skips low-fee buckets and returns a fee rate ≥ R, or `NoProperFeeRate` (triggering the fallback). [10](#0-9)

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L147-151)
```rust
    fn expire(&mut self) {
        let historical_blocks = Self::historical_blocks(constants::MAX_TARGET);
        let expired_tip = self.current_tip.saturating_sub(historical_blocks);
        self.txs.retain(|&num, _| num >= expired_tip);
    }
```

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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L205-215)
```rust
        let max_fee_rate = if let Some(fee_rate) = sorted_current_txs.first().map(|tx| tx.fee_rate)
        {
            fee_rate
        } else {
            return Ok(constants::LOWEST_FEE_RATE);
        };

        ckb_logger::debug!("max fee rate of current transactions: {max_fee_rate}");

        let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate);
        ckb_logger::debug!("current weight buckets size: {}", max_bucket_index + 1);
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L244-272)
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
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L279-298)
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
        }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L303-313)
```rust
    fn sorted_flowed(&self, historical_tip: BlockNumber) -> Vec<TxStatus> {
        let mut statuses: Vec<_> = self
            .txs
            .iter()
            .filter(|&(&num, _)| num >= historical_tip)
            .flat_map(|(_, statuses)| statuses.to_owned())
            .collect();
        statuses.sort_unstable_by(|a, b| b.cmp(a));
        ckb_logger::trace!(">>> sorted flowed length: {}", statuses.len());
        statuses
    }
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

**File:** util/fee-estimator/src/estimator/mod.rs (L83-89)
```rust
    /// Rejects a tx.
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
    }
```

**File:** shared/src/shared_builder.rs (L558-566)
```rust
    let fee_estimator_clone = fee_estimator.clone();
    tx_pool_builder.register_pending(Box::new(move |entry: &TxEntry| {
        // notify
        let notify_tx_entry = create_notify_entry(entry);
        notify_pending.notify_new_transaction(notify_tx_entry);
        let tx_hash = entry.transaction().hash();
        let entry_info = entry.to_info();
        fee_estimator_clone.accept_tx(tx_hash, entry_info);
    }));
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
