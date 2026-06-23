### Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

`remove_unconfirmed_tx` correctly guards the `block_unconfirmed_txs` array access for `tx_age >= 1000`, but applies **no equivalent guard** before indexing `confirm_blocks_to_failed_txs[tx_age - 1]`. When a tracked transaction is evicted from the tx-pool after more than 1000 blocks, `tx_age - 1 >= 1000` exceeds the array's length, causing a Rust index-out-of-bounds **panic** that crashes the node.

---

### Finding Description

`confirm_blocks_to_failed_txs` is allocated with length `MAX_CONFIRM_BLOCKS = 1000` (valid indices `0..=999`). [1](#0-0) 

In `remove_unconfirmed_tx`, the `block_unconfirmed_txs` path is guarded:

```rust
if tx_age >= self.block_unconfirmed_txs.len() {   // guards block_unconfirmed_txs only
    self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
} else { ... }
if count_failure {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64; // NO GUARD
}
``` [2](#0-1) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is out of bounds. The `block_unconfirmed_txs.len()` check on line 208 does not protect the `confirm_blocks_to_failed_txs` indexing on line 215.

The eviction path that sets `count_failure = true` is `drop_tx` → `drop_tx_inner(tx_hash, true)` → `remove_unconfirmed_tx(..., count_failure=true)`: [3](#0-2) 

`drop_tx` is called from `reject_tx`, which is invoked by the registered reject callback for every tx eviction (time expiry, pool size limit, conflict): [4](#0-3) 

A tx is tracked at `current_tip` height when it enters the pool: [5](#0-4) 

The default tx-pool expiry is 12 hours (`DEFAULT_EXPIRY_HOURS = 12`). At ~10 s/block, 1001 blocks ≈ 2.78 hours — well within the expiry window, so a tx can remain tracked for 1001+ blocks before eviction. [6](#0-5) 

---

### Impact Explanation

When the panic fires inside `algo.write().reject_tx(tx_hash)`, it unwinds through the fee estimator write-lock path and crashes the CKB node process. Any node operator who has opted into the `ConfirmationFraction` estimator and whose pool contains a tx that has aged past 1000 blocks is vulnerable.

---

### Likelihood Explanation

**Limiting factor — opt-in only.** The `ConfirmationFraction` estimator is not the default; the default is `FeeEstimator::Dummy` which is a no-op for `reject_tx`. The estimator is activated only when `fee_estimator.algorithm = "ConfirmationFraction"` is set in the node config (marked experimental in the changelog). [7](#0-6) [8](#0-7) 

For nodes that **have** enabled it: the trigger is passive — no special attacker action is needed beyond submitting a valid low-fee-rate transaction and waiting for 1001 blocks (~2.8 hours) without it being confirmed. Pool size pressure (`limit_size`) or time expiry (`remove_expired`) will then call `reject_tx` and hit the panic.

---

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` indexing, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
``` [9](#0-8) 

---

### Proof of Concept

```rust
// In TxConfirmStat unit test:
let mut stat = TxConfirmStat::new(buckets, 1000, decay);
// Simulate: tx entered at height 0, tip is now 1001 → tx_age = 1001
// count_failure = true (eviction path)
stat.add_unconfirmed_tx(0, some_fee_rate);
// This panics: index 1000 out of bounds for slice of length 1000
stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
```

The `tx_age - 1 = 1000` index exceeds `confirm_blocks_to_failed_txs.len() = 1000` (valid range `0..=999`), producing `thread 'main' panicked at 'index out of bounds: the len is 1000 but the index is 1000'`. [10](#0-9)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-141)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L197-217)
```rust
    fn remove_unconfirmed_tx(
        &mut self,
        entry_height: u64,
        tip_height: u64,
        bucket_index: usize,
        count_failure: bool,
    ) {
        let tx_age = tip_height.saturating_sub(entry_height) as usize;
        if tx_age < 1 {
            return;
        }
        if tx_age >= self.block_unconfirmed_txs.len() {
            self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
        } else {
            let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
            self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
        }
        if count_failure {
            self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L469-473)
```rust
    pub fn accept_tx(&mut self, tx_hash: Byte32, info: TxEntryInfo) {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        self.track_tx(tx_hash, fee_rate, self.current_tip)
    }
```

**File:** shared/src/shared_builder.rs (L406-414)
```rust
        let fee_estimator_algo = fee_estimator_config
            .map(|config| config.algorithm)
            .unwrap_or(None);
        let fee_estimator = match fee_estimator_algo {
            Some(FeeEstimatorAlgo::WeightUnitsFlow) => FeeEstimator::new_weight_units_flow(),
            Some(FeeEstimatorAlgo::ConfirmationFraction) => {
                FeeEstimator::new_confirmation_fraction()
            }
            None => FeeEstimator::new_dummy(),
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

**File:** util/app-config/src/legacy/tx_pool.rs (L17-18)
```rust
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
```

**File:** util/app-config/src/configs/fee_estimator.rs (L1-18)
```rust
use serde::{Deserialize, Serialize};

/// Fee estimator config options.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// The algorithm for fee estimator.
    pub algorithm: Option<Algorithm>,
}

/// Specifies the fee estimates algorithm.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Eq)]
pub enum Algorithm {
    /// Confirmation Fraction Fee Estimator
    ConfirmationFraction,
    /// Weight-Units Flow Fee Estimator
    WeightUnitsFlow,
}
```
