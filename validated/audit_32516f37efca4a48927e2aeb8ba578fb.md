### Title
`WeightUnitsFlow` Fee Estimator Retains Stale Flow Data After Transaction Removal — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

The `WeightUnitsFlow` fee estimator algorithm accumulates transaction weight data in its historical `txs` map when transactions enter the tx-pool, but **never removes that data** when transactions are evicted, expired, or replaced. The `FeeEstimator::reject_tx` dispatch explicitly no-ops for `WeightUnitsFlow`, leaving ghost weight entries that inflate the historical flow speed used by `estimate_fee_rate`. This is the direct CKB analog of the HydraDX M-07 pattern: a secondary data store is not cleaned up when the primary entity is removed, causing a derived metric to be incorrect.

---

### Finding Description

When a transaction enters the tx-pool and is accepted as pending, the `register_pending` callback fires and calls `fee_estimator.accept_tx(tx_hash, entry_info)`. [1](#0-0) 

For the `WeightUnitsFlow` algorithm, `accept_tx` appends the transaction's weight to `self.txs[current_tip]` — a `HashMap<BlockNumber, Vec<TxStatus>>` keyed by the block height at which the tx entered the pool: [2](#0-1) 

Later, when a transaction is removed from the pool for any reason (eviction due to pool-full, expiry, RBF replacement, or the `remove_transaction` RPC), the `register_reject` callback fires and calls `fee_estimator.reject_tx(&tx_hash)`: [3](#0-2) 

However, `FeeEstimator::reject_tx` explicitly **no-ops** for `WeightUnitsFlow`: [4](#0-3) 

The `WeightUnitsFlow::Algorithm` struct has no `reject_tx` method at all. The weight data added by `accept_tx` is never removed from `self.txs`. It persists until the entire block-window expires via `expire()`: [5](#0-4) 

The expiry window is `historical_blocks(MAX_TARGET) = MAX_TARGET * 2` blocks — a long window. During this entire period, the stale weight of removed transactions inflates the `flow_speed_buckets` used in `do_estimate`: [6](#0-5) 

The `sorted_flowed` helper reads all entries in `self.txs` within the historical window without any filtering for whether those transactions were actually confirmed: [7](#0-6) 

By contrast, the `ConfirmationFraction` algorithm correctly handles removal via `drop_tx_inner`, which removes the tx from `tracked_txs` and decrements the unconfirmed count in `block_unconfirmed_txs`: [8](#0-7) 

---

### Impact Explanation

The `estimate_fee_rate` RPC endpoint returns inflated fee rate estimates when the node is configured with the `WeightUnitsFlow` algorithm. Because the historical flow speed is computed from all transactions that ever entered the pool — including those that were evicted, expired, or replaced — the algorithm overestimates how fast new weight is entering the mempool. This causes `do_estimate` to conclude that more blocks are needed to drain the mempool, pushing the recommended fee rate higher than the true market rate. Users and wallets relying on this RPC to set transaction fees will systematically overpay. [9](#0-8) 

---

### Likelihood Explanation

The `WeightUnitsFlow` algorithm must be explicitly configured (the default is `Dummy`). However, once configured, the stale-data accumulation is triggered by any normal pool churn: transactions that are evicted when the pool is full (`Reject::Full`), transactions that time out (`Reject::Expiry`), or transactions replaced via RBF (`Reject::RBFRejected`). All of these are routine events on a live node. An unprivileged RPC caller or tx-pool submitter can amplify the effect by submitting many high-fee-rate transactions and then having them evicted (e.g., by submitting a conflicting transaction that spends the same input), inflating the flow data for the duration of the historical window. [10](#0-9) 

---

### Recommendation

Implement a `reject_tx` method on `weight_units_flow::Algorithm` that removes the corresponding `TxStatus` entry from `self.txs[entry_height]` when a transaction is dropped. This requires tracking per-tx metadata (entry height and weight) in a side map, analogous to `tracked_txs` in `ConfirmationFraction`. Then update `FeeEstimator::reject_tx` to dispatch to this new method instead of no-oping:

```rust
// In FeeEstimator::reject_tx:
Self::WeightUnitsFlow(algo) => algo.write().reject_tx(tx_hash),
``` [4](#0-3) 

---

### Proof of Concept

1. Configure the node with `fee_estimator.algorithm = "WeightUnitsFlow"`.
2. Submit a batch of high-fee-rate transactions to the tx-pool (via `send_transaction` RPC). Each accepted tx triggers `accept_tx`, adding its weight to `self.txs[current_tip]`.
3. Submit a conflicting transaction that spends the same inputs, causing the original transactions to be evicted via `resolve_conflict` → `call_reject` → `fee_estimator.reject_tx` (which is a no-op for `WeightUnitsFlow`).
4. Call `estimate_fee_rate`. The `sorted_flowed` function returns the weight of the evicted transactions as if they were still flowing into the mempool, inflating `flow_speed_buckets`.
5. Observe that the returned fee rate is higher than the actual market rate, because the algorithm believes the mempool is receiving more high-fee-rate weight per block than it actually is.

The stale data persists for `MAX_TARGET * 2` blocks before `expire()` cleans it up — the same "incorrect for a period then self-correcting" pattern identified in HydraDX M-07. [11](#0-10)

### Citations

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

**File:** shared/src/shared_builder.rs (L576-601)
```rust
    tx_pool_builder.register_reject(Box::new(
        move |tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject| {
            let tx_hash = entry.transaction().hash();
            // record recent reject
            if reject.should_recorded()
                && let Some(ref mut recent_reject) = tx_pool.recent_reject
                && let Err(e) = recent_reject.put(&tx_hash, reject.clone())
            {
                error!("record recent_reject failed {} {} {}", tx_hash, reject, e);
            }

            if reject.is_allowed_relay()
                && let Err(e) = tx_relay_sender.send(TxVerificationResult::Reject {
                    tx_hash: tx_hash.clone(),
                })
            {
                error!("tx-pool tx_relay_sender internal error {}", e);
            }

            // notify
            let notify_tx_entry = create_notify_entry(entry);
            notify_reject.notify_reject_transaction(notify_tx_entry, reject);

            // fee estimator
            fee_estimator.reject_tx(&tx_hash);
        },
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L67-74)
```rust
#[derive(Clone)]
pub struct Algorithm {
    boot_tip: BlockNumber,
    current_tip: BlockNumber,
    txs: HashMap<BlockNumber, Vec<TxStatus>>,

    is_ready: bool,
}
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L138-151)
```rust
    pub fn commit_block(&mut self, block: &BlockView) {
        let tip_number = block.number();
        if self.boot_tip == 0 {
            self.boot_tip = tip_number;
        }
        self.current_tip = tip_number;
        self.expire();
    }

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L416-430)
```rust
    fn drop_tx_inner(&mut self, tx_hash: &Byte32, count_failure: bool) -> Option<TxRecord> {
        self.tracked_txs.remove(tx_hash).inspect(|tx_record| {
            self.tx_confirm_stat.remove_unconfirmed_tx(
                tx_record.height,
                self.best_height,
                tx_record.bucket_index,
                count_failure,
            );
        })
    }

    /// tx removed from txpool
    fn drop_tx(&mut self, tx_hash: &Byte32) -> bool {
        self.drop_tx_inner(tx_hash, true).is_some()
    }
```
