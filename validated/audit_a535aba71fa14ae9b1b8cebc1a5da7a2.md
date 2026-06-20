### Title
WeightUnitsFlow Fee Estimator Historical Flow Data Can Be Inflated at Zero Cost via Submit-then-Remove - (`util/fee-estimator/src/estimator/mod.rs`)

### Summary

The `WeightUnitsFlow` fee estimator records every transaction that enters the tx-pool into permanent historical flow data (`self.txs`), but the corresponding `reject_tx` path is a deliberate no-op. An unprivileged RPC caller can submit a transaction with an arbitrarily high fee rate, have it recorded in the flow history, then immediately remove it via the `remove_transaction` RPC — at zero net cost — permanently inflating the estimated flow speed for high-fee-rate buckets. Repeating this across multiple blocks causes `estimate_fee_rate` to return fee rates far above the true market rate, harming every wallet or application that relies on the node's fee oracle.

### Finding Description

`FeeEstimator::reject_tx` in `util/fee-estimator/src/estimator/mod.rs` is explicitly a no-op for the `WeightUnitsFlow` variant:

```rust
pub fn reject_tx(&self, tx_hash: &Byte32) {
    match self {
        Self::Dummy | Self::WeightUnitsFlow(_) => {}   // ← no-op
        Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
    }
}
``` [1](#0-0) 

Yet `accept_tx` for the same variant **does** record the transaction into `self.txs`, keyed by the current chain tip height:

```rust
pub fn accept_tx(&self, tx_hash: Byte32, info: TxEntryInfo) {
    match self {
        Self::Dummy => {}
        Self::ConfirmationFraction(algo) => algo.write().accept_tx(tx_hash, info),
        Self::WeightUnitsFlow(algo) => algo.write().accept_tx(info),  // ← records
    }
}
``` [2](#0-1) 

Inside `WeightUnitsFlow::accept_tx`, the transaction's weight and fee rate are appended to `self.txs[current_tip]`: [3](#0-2) 

This historical data is later used in `do_estimate` to compute `flow_speed_buckets` — the rate at which new weight enters each fee-rate bucket per block: [4](#0-3) 

The estimation decision is:

```rust
let added_weight = flow_speed_buckets[bucket_index] * target_blocks;
let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
let passed = current_weight + added_weight <= removed_weight;
``` [5](#0-4) 

Inflating `flow_speed_buckets` raises `added_weight`, causing more low-fee-rate buckets to fail the `passed` test, so the algorithm returns a higher fee-rate bucket as its estimate.

The reject callback registered in `shared_builder.rs` does call `fee_estimator.reject_tx`, but because that function is a no-op for `WeightUnitsFlow`, the historical data is never corrected regardless of how the transaction leaves the pool (manual removal, eviction, RBF): [6](#0-5) 

The `remove_transaction` RPC (backed by `TxPoolController::remove_local_tx`) is a standard, unauthenticated pool RPC endpoint: [7](#0-6) 

### Impact Explanation

Any wallet, exchange, or dApp that calls `estimate_fee_rate` on a manipulated node will receive an inflated fee rate and overpay transaction fees. Because the `WeightUnitsFlow` algorithm uses a sliding window of `historical_blocks` (up to `MAX_TARGET * 2` blocks), the poisoned data persists for a long time before expiring via `expire()`. The attacker can continuously re-poison each new block height to maintain the inflated estimate indefinitely. This is a fee-oracle manipulation attack: users are systematically overcharged, and the node's fee estimate becomes unreliable as a market signal. [8](#0-7) 

### Likelihood Explanation

The attack requires only:
1. Owning any live cell (to construct a valid transaction).
2. Access to the node's JSON-RPC endpoint (standard for any public or semi-public node).
3. Calling `send_transaction` followed by `remove_transaction` in a loop — no mining power, no capital lock-up, no fee payment (transactions are never confirmed).

The cost is gas-equivalent: only the CPU/network overhead of RPC calls. The `remove_transaction` RPC is not gated behind any special permission in the Pool module. This is directly analogous to the Aloe IV manipulation: deposit → update → withdraw at zero cost.

### Recommendation

Implement `reject_tx` for the `WeightUnitsFlow` algorithm to remove the corresponding entry from `self.txs` when a transaction is evicted or manually removed. Since `self.txs` is keyed by block height and stores a `Vec<TxStatus>` (not individual tx hashes), the algorithm needs to either:

- Track individual transactions by hash (as `ConfirmationFraction` does with `tracked_txs`), or
- At minimum, subtract the evicted transaction's weight from the appropriate `self.txs[height]` bucket entry.

Additionally, consider rate-limiting the `remove_transaction` RPC or restricting it to localhost/authenticated callers, since it has no legitimate use case for untrusted external callers.

### Proof of Concept

```
# Attacker owns cell at outpoint X

# Step 1: Submit a transaction with a very high fee rate (e.g., 2,000,000 shannons/KB)
# → WeightUnitsFlow::accept_tx records it in self.txs[current_tip]
POST /rpc  {"method": "send_transaction", "params": [high_fee_tx]}
# Returns tx_hash

# Step 2: Immediately remove it — zero cost, cells not consumed
POST /rpc  {"method": "remove_transaction", "params": [tx_hash]}
# → reject_tx is called but is a no-op for WeightUnitsFlow
# → self.txs[current_tip] still contains the high-fee-rate entry

# Step 3: Repeat once per block for historical_blocks (e.g., 262 blocks for HIGH_TARGET)
# to saturate the flow history with phantom high-fee transactions

# Step 4: Any caller now gets an inflated estimate
POST /rpc  {"method": "estimate_fee_rate", "params": []}
# Returns 2,000,000 shannons/KB instead of the true ~1,000 shannons/KB
```

The attacker's cells are never spent. The only cost is RPC call overhead. The poisoned data persists until `expire()` evicts it after `historical_blocks` new blocks are committed. [3](#0-2) [1](#0-0)

### Citations

**File:** util/fee-estimator/src/estimator/mod.rs (L75-81)
```rust
    pub fn accept_tx(&self, tx_hash: Byte32, info: TxEntryInfo) {
        match self {
            Self::Dummy => {}
            Self::ConfirmationFraction(algo) => algo.write().accept_tx(tx_hash, info),
            Self::WeightUnitsFlow(algo) => algo.write().accept_tx(info),
        }
    }
```

**File:** util/fee-estimator/src/estimator/mod.rs (L84-89)
```rust
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
    }
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

**File:** shared/src/shared_builder.rs (L576-602)
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
    ));
```

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
    }
```
