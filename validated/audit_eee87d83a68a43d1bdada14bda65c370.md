### Title
WeightUnitsFlow Fee Estimator Historical Flow Data Poisoning via Untracked Rejected Transactions — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

The `WeightUnitsFlow` fee estimator records every transaction admitted to the tx-pool in its historical flow table (`self.txs`) via `accept_tx`, but **never removes entries when transactions are subsequently rejected, evicted, or manually removed**. The `reject_tx` handler for `WeightUnitsFlow` is an explicit no-op. An unprivileged attacker can submit a batch of high-fee-rate transactions, immediately remove them from the pool, and leave permanently inflated historical flow data that causes `estimate_fee_rate` to return an artificially elevated fee rate for up to `historical_blocks` (~256) blocks. This is the direct analog of the BondVault sandwich attack: an attacker manipulates a spot-price-like oracle (the fee rate estimator) by injecting and withdrawing artificial data, causing the system to report bad prices that downstream users act on.

---

### Finding Description

**Root cause — `reject_tx` is a no-op for `WeightUnitsFlow`**

In `util/fee-estimator/src/estimator/mod.rs`, the `FeeEstimator::reject_tx` dispatch explicitly skips `WeightUnitsFlow`:

```rust
pub fn reject_tx(&self, tx_hash: &Byte32) {
    match self {
        Self::Dummy | Self::WeightUnitsFlow(_) => {}          // ← no-op
        Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
    }
}
``` [1](#0-0) 

The `ConfirmationFraction` variant correctly removes tracked transactions on rejection; `WeightUnitsFlow` does not.

**How `accept_tx` populates the historical table**

Every transaction admitted to the pending pool triggers `accept_tx` via the `register_pending` callback in `shared/src/shared_builder.rs`:

```rust
tx_pool_builder.register_pending(Box::new(move |entry: &TxEntry| {
    // ...
    fee_estimator_clone.accept_tx(tx_hash, entry_info);
}));
``` [2](#0-1) 

Inside `WeightUnitsFlow::accept_tx`, the transaction's weight and fee rate are appended to `self.txs[current_tip]`:

```rust
pub fn accept_tx(&mut self, info: TxEntryInfo) {
    if self.current_tip == 0 { return; }
    let item = TxStatus::new_from_entry_info(info);
    self.txs
        .entry(self.current_tip)
        .and_modify(|items| items.push(item))
        .or_insert_with(|| vec![item]);
}
``` [3](#0-2) 

**How the poisoned data inflates the estimate**

`estimate_fee_rate` builds `flow_speed_buckets` from `self.txs` (all historically recorded transactions, including already-removed ones):

```rust
let sorted_flowed = self.sorted_flowed(historical_tip);
// ...
buckets.into_iter()
    .map(|value| value / historical_blocks)
    .collect::<Vec<_>>()
``` [4](#0-3) 

The decision loop then checks, for each fee-rate bucket from lowest to highest:

```rust
let added_weight = flow_speed_buckets[bucket_index] * target_blocks;
let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
let passed = current_weight + added_weight <= removed_weight;
if passed { return Ok(fee_rate); }
``` [5](#0-4) 

If `flow_speed_buckets` is artificially inflated at high fee-rate buckets, `added_weight` for those buckets grows, preventing low-fee-rate buckets from passing the check, and the returned estimate is pushed upward.

**The reject callback fires but has no effect**

When a transaction is removed from the pool (via `remove_transaction` RPC, eviction, or expiry), the `register_reject` callback fires and calls `fee_estimator.reject_tx(&tx_hash)`:

```rust
tx_pool_builder.register_reject(Box::new(
    move |tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject| {
        // ...
        fee_estimator.reject_tx(&tx_hash);
    },
));
``` [6](#0-5) 

Because `WeightUnitsFlow::reject_tx` is a no-op, the entry in `self.txs` is never cleaned up. The poisoned flow data persists until `expire()` removes it after `historical_blocks` blocks (up to 256 blocks, ~2 hours). [7](#0-6) 

---

### Impact Explanation

Any wallet, dApp, or automated transaction sender that calls `estimate_fee_rate` (RPC method in the `Experiment` module) and uses the result to set transaction fees will be misled into overpaying fees. The inflated estimate persists for up to `historical_blocks` (~256) blocks per attack cycle. While fees go to miners rather than the attacker, the economic harm to users is real and repeatable. The attack is analogous to the BondVault sandwich: an attacker injects artificial high-fee-rate "flow" into the oracle, causing the oracle to report a bad price, then withdraws the artificial data from the live pool while the historical record remains poisoned.

---

### Likelihood Explanation

The attack requires only:
1. The ability to call `send_transaction` (publicly accessible RPC, no privilege required)
2. The ability to call `remove_transaction` (publicly accessible Pool RPC)
3. Sufficient CKB to pay the minimum fee rate on the injected transactions

The cost per attack cycle is the minimum fee rate multiplied by the number of injected transactions. The attacker can repeat the attack every ~256 blocks to maintain the inflated estimate indefinitely. No privileged access, no 51% hashpower, and no social engineering are required.

---

### Recommendation

Implement `reject_tx` for the `WeightUnitsFlow` estimator to remove transactions from `self.txs` when they leave the pool without being committed. Since `self.txs` is keyed by block number and stores `Vec<TxStatus>` (not by tx hash), the simplest fix is to store entries keyed by tx hash alongside the block number, enabling O(1) removal. Alternatively, store a separate `HashMap<Byte32, (BlockNumber, TxStatus)>` for pending entries and remove from `self.txs` on rejection, mirroring the `ConfirmationFraction` approach.

---

### Proof of Concept

1. Configure the node to use `WeightUnitsFlow` fee estimator (`fee_estimator.algorithm = "WeightUnitsFlow"` in config).
2. Wait for the estimator to become ready (requires `historical_blocks` blocks after boot).
3. Record the baseline `estimate_fee_rate` result (e.g., 1000 shannons/KB).
4. Submit 500 transactions via `send_transaction` RPC, each with a fee rate of 2,000,000 shannons/KB (maximum bucket range) and maximum allowed size. These are recorded in `self.txs[current_tip]`.
5. Immediately call `remove_transaction` for each submitted transaction. They leave the live pool but remain in `self.txs`.
6. Call `estimate_fee_rate` again. The returned value will be significantly higher than the baseline because `flow_speed_buckets` at high fee-rate indices is now inflated, preventing low-fee-rate buckets from passing the `current_weight + added_weight <= removed_weight` check.
7. Observe that the inflated estimate persists for up to `historical_blocks` (~256) blocks before `expire()` cleans the poisoned entries.

### Citations

**File:** util/fee-estimator/src/estimator/mod.rs (L84-88)
```rust
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
```

**File:** shared/src/shared_builder.rs (L559-566)
```rust
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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L245-271)
```rust
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
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L279-297)
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
```
