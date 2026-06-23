### Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age` Exceeds Circular Buffer Size — (`File: util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

The `ConfirmationFraction` fee estimator uses a fixed-size circular buffer (`block_unconfirmed_txs`, length 1000) to track unconfirmed transactions. When a tracked transaction is dropped/rejected after more than 1000 blocks, `remove_unconfirmed_tx` correctly routes the `block_unconfirmed_txs` decrement to `old_unconfirmed_txs`, but unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without a matching bounds guard. Since `confirm_blocks_to_failed_txs` also has length 1000, any `tx_age > 1000` causes a Rust index-out-of-bounds **panic**, crashing the node.

---

### Finding Description

`TxConfirmStat` maintains two parallel fixed-size arrays, both of length `MAX_CONFIRM_BLOCKS = 1000`:

- `block_unconfirmed_txs: Vec<Vec<usize>>` — circular ring buffer indexed by `entry_height % 1000`
- `confirm_blocks_to_failed_txs: Vec<Vec<f64>>` — indexed by `tx_age - 1`

In `remove_unconfirmed_tx`:

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
    if tx_age >= self.block_unconfirmed_txs.len() {   // ← guard for ring buffer
        self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
    } else {
        let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
        self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
    }
    if count_failure {
        self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;  // ← NO guard
    }
}
```

The guard `if tx_age >= self.block_unconfirmed_txs.len()` protects the ring buffer access, but the `confirm_blocks_to_failed_txs[tx_age - 1]` access on line 215 has **no corresponding bounds check**. When `tx_age > 1000`, `tx_age - 1 >= 1000` is out of bounds for a `Vec` of length 1000. Rust panics unconditionally on out-of-bounds indexing in both debug and release builds. [1](#0-0) 

`count_failure = true` is passed by `drop_tx` → `drop_tx_inner(tx_hash, true)`: [2](#0-1) 

`reject_tx` calls `drop_tx`, and `reject_tx` is wired into the tx-pool reject callback in `register_tx_pool_callback`: [3](#0-2) 

The reject callback fires from `remove_expired` (tx expires after `expiry_hours`, default 12 hours) and `limit_size` (pool eviction): [4](#0-3) 

---

### Impact Explanation

When the `ConfirmationFraction` fee estimator is enabled and a tracked transaction remains in the pool for more than 1000 blocks (~2.8 hours at 10 s/block) before being rejected/evicted, the node **panics and crashes**. This is a remotely-triggerable denial-of-service: any tx-pool submitter can cause it by submitting a transaction that stays unconfirmed long enough to be evicted by the expiry timer (default 12 hours >> 1000 blocks).

---

### Likelihood Explanation

- The `ConfirmationFraction` algorithm is an opt-in configuration (`fee_estimator.algorithm = "ConfirmationFraction"` in `ckb.toml`), not the default.
- Once enabled, the trigger condition is trivially met: submit a low-fee transaction, wait ~2.8 hours (1000 blocks), and the node crashes when the tx expires at the 12-hour mark.
- No special privileges are required beyond the ability to submit a transaction via the `send_transaction` RPC. [5](#0-4) [6](#0-5) 

---

### Recommendation

Add a bounds guard before indexing `confirm_blocks_to_failed_txs`, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This matches the structural fix analogous to the Uniswap report: just as `Oracle.observe` must check `initialized` before using an observation, `remove_unconfirmed_tx` must check `tx_age` is within bounds before indexing `confirm_blocks_to_failed_txs`.

---

### Proof of Concept

1. Configure the node with `fee_estimator.algorithm = "ConfirmationFraction"`.
2. Submit a transaction with a fee rate just above `min_fee_rate` but low enough that miners do not include it. The tx is accepted into the pool and tracked at height `H`.
3. Wait for 1001+ blocks to be produced (~2.8 hours).
4. Either wait for the 12-hour expiry timer to fire (`remove_expired` → `callbacks.call_reject` → `fee_estimator.reject_tx`), or fill the pool to trigger `limit_size` eviction.
5. `remove_unconfirmed_tx` is called with `tx_age > 1000` and `count_failure = true`.
6. `confirm_blocks_to_failed_txs[tx_age - 1]` panics: **thread 'main' panicked at 'index out of bounds: the len is 1000 but the index is N'**, crashing the node. [7](#0-6) [8](#0-7)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L21-22)
```rust
/// The number of blocks that the estimator will trace the statistics.
const MAX_CONFIRM_BLOCKS: usize = 1000;
```

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

**File:** tx-pool/src/pool.rs (L271-288)
```rust
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
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
