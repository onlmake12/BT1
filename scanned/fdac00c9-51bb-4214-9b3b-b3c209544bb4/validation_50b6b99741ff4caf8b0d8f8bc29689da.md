### Title
`WeightUnitsFlow` Fee Estimator Ignores Transaction Removal, Enabling Persistent Fee-Rate Inflation via RBF — (`util/fee-estimator/src/estimator/mod.rs`, `util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

The `WeightUnitsFlow` fee estimator records every transaction that enters the pending pool into a persistent historical-flow map (`self.txs`), but **never removes entries when transactions are rejected or replaced**. An unprivileged RPC caller can exploit this by repeatedly submitting high-fee transactions and replacing them via RBF, causing the historical flow data to accumulate ghost entries that inflate the `estimate_fee_rate` output for up to ~256 blocks (~2 hours). This is a direct analog to the `RAACMinter::updateEmissionRate()` vulnerability: transient state manipulation (borrow/repay ↔ submit/RBF-replace) permanently shifts a persistent rate parameter (emission rate ↔ fee-rate estimate).

---

### Finding Description

**Root cause — asymmetric accept/reject in `WeightUnitsFlow`:**

`FeeEstimator::reject_tx` explicitly no-ops for the `WeightUnitsFlow` variant:

```rust
// util/fee-estimator/src/estimator/mod.rs
pub fn reject_tx(&self, tx_hash: &Byte32) {
    match self {
        Self::Dummy | Self::WeightUnitsFlow(_) => {}          // ← no-op
        Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
    }
}
``` [1](#0-0) 

But `accept_tx` **does** record the transaction for `WeightUnitsFlow`:

```rust
pub fn accept_tx(&self, tx_hash: Byte32, info: TxEntryInfo) {
    match self {
        Self::Dummy => {}
        Self::ConfirmationFraction(algo) => algo.write().accept_tx(tx_hash, info),
        Self::WeightUnitsFlow(algo) => algo.write().accept_tx(info),   // ← records
    }
}
``` [2](#0-1) 

`WeightUnitsFlow::accept_tx` appends the transaction's weight/fee-rate into `self.txs[current_tip]`: [3](#0-2) 

There is no corresponding removal path. The `WeightUnitsFlow` struct has no `reject_tx` method at all.

**How the callbacks are wired:**

In `shared/src/shared_builder.rs`, the `register_pending` callback calls `accept_tx` when a transaction enters the pool: [4](#0-3) 

The `register_reject` callback calls `reject_tx` (a no-op for `WeightUnitsFlow`) when a transaction is removed for any reason — including RBF replacement: [5](#0-4) 

**RBF explicitly fires the reject callback for replaced transactions:**

In `tx-pool/src/process.rs`, `process_rbf` removes the old transaction and calls `call_reject`: [6](#0-5) 

**How `estimate_fee_rate` uses the inflated data:**

`do_estimate` reads `self.txs` via `sorted_flowed` to compute a per-block "flow speed" for each fee-rate bucket. Inflated entries in `self.txs` directly increase the computed flow speed, causing the estimator to conclude that high-fee transactions are arriving faster than they actually are, and therefore return a higher recommended fee rate: [7](#0-6) 

**Persistence window:**

`expire()` retains entries for `historical_blocks(MAX_TARGET) = MAX_TARGET * 2 = 256` blocks. With an average block interval of 28 s, inflated data persists for ~2 hours: [8](#0-7) [9](#0-8) 

---

### Impact Explanation

Every call to the `estimate_fee_rate` RPC that uses the `WeightUnitsFlow` algorithm returns an inflated fee rate. Wallets and users who rely on this estimate will overpay transaction fees. The attacker can sustain the inflation for the full 256-block window by repeating the attack at each new block tip. The `ConfirmationFraction` estimator is unaffected because it correctly calls `drop_tx` on rejection. [10](#0-9) 

---

### Likelihood Explanation

The attack requires only:
1. Access to the node's RPC endpoint (standard for any tx submitter).
2. Owning a UTXO to fund the initial transaction.
3. Repeatedly submitting RBF replacements — each replacement costs only the minimum RBF fee increment (`min_rbf_rate` × tx size, typically a few hundred shannons).

No mining power, privileged access, or special role is needed. The `send_transaction` RPC is publicly reachable on any node that exposes its RPC port.

---

### Recommendation

Implement a proper `reject_tx` method on `weight_units_flow::Algorithm` that removes the corresponding entry from `self.txs` when a transaction is rejected or replaced. Mirror the approach used by `ConfirmationFraction::drop_tx`. Wire it through `FeeEstimator::reject_tx` instead of the current no-op:

```rust
// In weight_units_flow::Algorithm
pub fn reject_tx(&mut self, tip: BlockNumber, fee_rate: FeeRate) {
    if let Some(entries) = self.txs.get_mut(&tip) {
        entries.retain(|s| s.fee_rate != fee_rate);
    }
}
```

Alternatively, track transactions by hash (as `ConfirmationFraction` does) to allow precise removal.

---

### Proof of Concept

1. Node is configured with `WeightUnitsFlow` fee estimator.
2. Attacker owns UTXO `U` worth 100 CKB.
3. Attacker submits `tx1` spending `U`, output = 99.9 CKB (fee = 0.1 CKB, high fee-rate). → `accept_tx` records `tx1` in `self.txs[tip]`.
4. Attacker submits `tx2` spending `U`, output = 99.8 CKB (fee = 0.2 CKB, RBF replaces `tx1`). → `call_reject` fires for `tx1` → no-op for `WeightUnitsFlow` → `tx1` entry stays in `self.txs`. → `accept_tx` records `tx2` in `self.txs[tip]`.
5. Repeat step 4 N times. `self.txs[tip]` now contains N+1 high-fee entries for a single UTXO.
6. Call `estimate_fee_rate` → `sorted_flowed` returns all N+1 ghost entries → flow speed is N+1× inflated → returned fee rate is inflated.
7. Entries persist for 256 blocks. Attacker repeats at each new tip to maintain inflation.

### Citations

**File:** util/fee-estimator/src/estimator/mod.rs (L74-81)
```rust
    /// Accepts a tx.
    pub fn accept_tx(&self, tx_hash: Byte32, info: TxEntryInfo) {
        match self {
            Self::Dummy => {}
            Self::ConfirmationFraction(algo) => algo.write().accept_tx(tx_hash, info),
            Self::WeightUnitsFlow(algo) => algo.write().accept_tx(info),
        }
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

**File:** shared/src/shared_builder.rs (L575-602)
```rust
    let notify_reject = notify;
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

**File:** tx-pool/src/process.rs (L219-232)
```rust
        for old in all_removed {
            debug!(
                "remove conflict tx {} for RBF by new tx {}",
                old.transaction().hash(),
                entry.transaction().hash()
            );
            let reject =
                Reject::RBFRejected(format!("replaced by tx {}", entry.transaction().hash()));

            // RBF replace successfully, put old transactions into conflicts pool
            tx_pool.record_conflict(old.transaction().clone());
            // after removing old tx from tx_pool, we call reject callbacks manually
            self.callbacks.call_reject(tx_pool, &old, reject);
        }
```

**File:** util/fee-estimator/src/constants.rs (L9-10)
```rust
/// Max target blocks, about 1 hour (128).
pub(crate) const MAX_TARGET: BlockNumber = (60 * 60) / AVG_BLOCK_INTERVAL;
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L475-477)
```rust
    pub fn reject_tx(&mut self, tx_hash: &Byte32) {
        let _ = self.drop_tx(tx_hash);
    }
```
