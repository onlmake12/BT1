### Title
Attacker Can Inflate `WeightUnitsFlow` Fee Estimator Historical Flow Data via Rejected Transactions — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

The `WeightUnitsFlow` fee estimator records every accepted transaction's weight in a historical flow map (`self.txs`), but never removes entries when transactions are rejected or evicted from the pool. Because `reject_tx` is a deliberate no-op for this algorithm, an attacker can submit high-fee-rate transactions, trigger their eviction (via RBF, pool-size eviction, or expiry), and permanently inflate the historical `flow_speed_buckets` for the duration of the estimator's lookback window (~256 blocks). Any user who calls `estimate_fee_rate` during this window receives an artificially elevated fee rate recommendation and overpays.

---

### Finding Description

**Root cause — `reject_tx` is a no-op for `WeightUnitsFlow`**

In `util/fee-estimator/src/estimator/mod.rs`, the `reject_tx` dispatcher explicitly skips the `WeightUnitsFlow` variant:

```rust
pub fn reject_tx(&self, tx_hash: &Byte32) {
    match self {
        Self::Dummy | Self::WeightUnitsFlow(_) => {}   // ← no-op
        Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
    }
}
``` [1](#0-0) 

Every time a transaction is accepted into the pending pool, the callback registered in `shared_builder.rs` calls `fee_estimator.accept_tx`, which records the transaction's weight and fee rate in `self.txs` keyed by the current block number: [2](#0-1) 

```rust
pub fn accept_tx(&mut self, info: TxEntryInfo) {
    let item = TxStatus::new_from_entry_info(info);
    self.txs
        .entry(self.current_tip)
        .and_modify(|items| items.push(item))
        .or_insert_with(|| vec![item]);
}
``` [3](#0-2) 

When a transaction is later evicted (pool-size limit, expiry, or RBF), `TxPool::limit_size` calls `callbacks.call_reject`, which dispatches to `fee_estimator.reject_tx` — but for `WeightUnitsFlow` this is a no-op, so the entry in `self.txs` is never removed. [4](#0-3) 

**How the inflated data propagates to the fee estimate**

`do_estimate` builds `flow_speed_buckets` by summing all weights in `self.txs` over the last `historical_blocks` blocks and dividing by `historical_blocks`:

```rust
buckets.into_iter()
    .map(|value| value / historical_blocks)
    .collect::<Vec<_>>()
``` [5](#0-4) 

For each fee-rate bucket the estimator checks:

```rust
let passed = current_weight + added_weight <= removed_weight;
```

where `added_weight = flow_speed_buckets[bucket_index] * target_blocks` and `removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks`. [6](#0-5) 

Inflating `flow_speed_buckets` for high-fee-rate buckets causes low-fee-rate buckets to fail the `passed` check, pushing the returned fee rate upward. The effect persists for up to `historical_blocks` = `MAX_TARGET * 2` = 256 blocks (~2 hours). [7](#0-6) 

**Concrete attack path (RBF double-count)**

1. Attacker submits a batch of transactions with a high fee rate. Each is accepted → `accept_tx` records their weight in `self.txs[current_tip]`.
2. Attacker immediately submits conflicting replacements (RBF) with a marginally higher fee rate. The first batch is evicted → `reject_tx` is called but is a no-op for `WeightUnitsFlow`. The second batch is also accepted → `accept_tx` records their weight again.
3. Both batches' weights now exist in `self.txs`, doubling the apparent flow speed for those fee-rate buckets.
4. The attacker can repeat this cycle, each time paying only the marginal RBF fee increment while multiplying the recorded flow weight.
5. For the next 256 blocks, `estimate_fee_rate` returns an inflated recommendation to every caller.

The RBF eviction path is confirmed in `check_rbf` / `resolve_conflict`, which calls `callbacks.call_reject` for displaced entries. [8](#0-7) 

The `estimate_fee_rate` RPC is publicly reachable: [9](#0-8) 

---

### Impact Explanation

Users and wallets that call `estimate_fee_rate` to set transaction fees will receive an artificially high fee rate recommendation. They will overpay miners for transaction inclusion. The effect is sustained for up to 256 blocks (~2 hours per attack cycle) and can be renewed continuously at low marginal cost to the attacker. This is a direct loss of funds for honest users.

---

### Likelihood Explanation

The attack requires only the ability to submit valid transactions to the pool — an unprivileged capability available to any RPC caller or P2P peer. The attacker must pay the marginal RBF fee increment per cycle, but can amplify the recorded flow weight by an arbitrary multiplier. The `WeightUnitsFlow` algorithm is a configurable option; nodes that enable it are fully exposed. The `ConfirmationFraction` algorithm is not affected because it correctly implements `reject_tx`. [1](#0-0) 

---

### Recommendation

Implement a `reject_tx` handler for `WeightUnitsFlow` that removes the corresponding `TxStatus` entry from `self.txs` when a transaction is rejected or evicted. Because `self.txs` is keyed by block number and stores a `Vec<TxStatus>` without per-tx identity, the simplest fix is to store entries keyed by `tx_hash` (as `ConfirmationFraction` does) so they can be individually removed on rejection.

Alternatively, cap the maximum weight contribution per block in `self.txs` to bound the inflation a single attacker can cause.

---

### Proof of Concept

```
1. Node configured with WeightUnitsFlow fee estimator.
2. Attacker calls send_transaction(tx_A, fee_rate=HIGH) → accepted, weight W recorded in self.txs[tip].
3. Attacker calls send_transaction(tx_A_rbf, fee_rate=HIGH+1, same inputs) → tx_A evicted (reject_tx no-op), tx_A_rbf accepted, weight W recorded again in self.txs[tip].
4. Repeat step 3 N times. self.txs[tip] now contains N+1 copies of weight W for the high fee-rate bucket.
5. Any call to estimate_fee_rate within the next 256 blocks returns a fee rate elevated by the inflated flow_speed_buckets, causing honest users to overpay.
```

The no-op `reject_tx` for `WeightUnitsFlow` is the necessary and sufficient root cause: [10](#0-9) [3](#0-2) [6](#0-5)

### Citations

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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L268-272)
```rust
            buckets
                .into_iter()
                .map(|value| value / historical_blocks)
                .collect::<Vec<_>>()
        };
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

**File:** tx-pool/src/pool.rs (L290-328)
```rust
    // Remove transactions from the pool until total size <= size_limit.
    // Return a `Reject` for current inserting entry if it's removed
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
```

**File:** tx-pool/src/pool.rs (L574-646)
```rust
    pub(crate) fn check_rbf(
        &self,
        snapshot: &Snapshot,
        entry: &TxEntry,
    ) -> Result<HashSet<ProposalShortId>, Reject> {
        assert!(self.enable_rbf());
        let tx_inputs: Vec<OutPoint> = entry.transaction().input_pts_iter().collect();
        let conflict_ids = self.pool_map.find_conflict_tx(entry.transaction());

        if conflict_ids.is_empty() {
            return Ok(HashSet::new());
        }

        let short_id = entry.proposal_short_id();

        // Rule #1, the node has enabled RBF, which is checked by caller
        let conflicts = conflict_ids
            .iter()
            .filter_map(|id| self.get_pool_entry(id))
            .collect::<Vec<_>>();
        assert!(conflicts.len() == conflict_ids.len());

        // Rule #2, new tx don't contain any new unconfirmed inputs
        let mut inputs = HashSet::new();
        for c in conflicts.iter() {
            inputs.extend(c.inner.transaction().input_pts_iter());
        }

        if tx_inputs
            .iter()
            .any(|pt| !inputs.contains(pt) && !snapshot.transaction_exists(&pt.tx_hash()))
        {
            return Err(Reject::RBFRejected(
                "new Tx contains unconfirmed inputs".to_string(),
            ));
        }

        // Rule #5, the replaced tx's descendants can not more than 100
        // and the ancestor of the new tx don't have common set with the replaced tx's descendants
        let mut replace_count: usize = 0;
        let mut all_conflicted = conflicts.clone();
        let ancestors = self.pool_map.calc_ancestors(&short_id);
        for conflict in conflicts.iter() {
            let descendants = self.pool_map.calc_descendants(&conflict.id);
            replace_count += descendants.len() + 1;
            if replace_count > MAX_REPLACEMENT_CANDIDATES {
                return Err(Reject::RBFRejected(format!(
                    "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
                    replace_count, MAX_REPLACEMENT_CANDIDATES,
                )));
            }

            if !descendants.is_disjoint(&ancestors) {
                return Err(Reject::RBFRejected(
                    "Tx ancestors have common with conflict Tx descendants".to_string(),
                ));
            }

            let entries = descendants
                .iter()
                .filter_map(|id| self.get_pool_entry(id))
                .collect::<Vec<_>>();

            for entry in entries.iter() {
                let hash = entry.inner.transaction().hash();
                if tx_inputs.iter().any(|pt| pt.tx_hash() == hash) {
                    return Err(Reject::RBFRejected(
                        "new Tx contains inputs in descendants of to be replaced Tx".to_string(),
                    ));
                }
            }
            all_conflicted.extend(entries);
        }
```

**File:** util/fee-estimator/src/constants.rs (L10-10)
```rust
pub(crate) const MAX_TARGET: BlockNumber = (60 * 60) / AVG_BLOCK_INTERVAL;
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
