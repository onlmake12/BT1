### Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

When the `ConfirmationFraction` fee estimator is enabled, a transaction that remains in the tx-pool for more than 1000 blocks and is then evicted (rejected) causes an unconditional out-of-bounds Vec index at line 215, panicking the node process.

---

### Finding Description

`TxConfirmStat::remove_unconfirmed_tx` computes `tx_age = tip_height - entry_height`. It correctly guards the `block_unconfirmed_txs` access at line 208 against `tx_age >= 1000`, but applies **no equivalent guard** before indexing `confirm_blocks_to_failed_txs` at line 215: [1](#0-0) 

`confirm_blocks_to_failed_txs` is allocated with exactly `max_confirm_blocks = 1000` outer entries: [2](#0-1) 

When `tx_age = 1001`, `tx_age - 1 = 1000`, which is one past the last valid index (0–999). Rust's `Vec` indexing panics on out-of-bounds in release builds.

The guard at line 208 only protects the `block_unconfirmed_txs` branch; the `count_failure` branch at line 214–216 has no corresponding `tx_age <= self.confirm_blocks_to_failed_txs.len()` check. [3](#0-2) 

---

### Impact Explanation

A Rust `index out of bounds` panic in the fee estimator thread propagates and crashes the CKB node process. Any node operator running with `algorithm = "ConfirmationFraction"` is affected. The crash is deterministic and repeatable.

---

### Likelihood Explanation

**Precondition:** The `ConfirmationFraction` estimator must be explicitly enabled in `ckb.toml` (`[fee_estimator] algorithm = "ConfirmationFraction"`). The default is `Dummy`. [4](#0-3) 

**Trigger path:**

1. Attacker submits a valid low-fee-rate transaction via RPC or P2P relay → `accept_tx` → `track_tx` records `entry_height = best_height`. [5](#0-4) 

2. The tx is never confirmed (low fee-rate, pool congestion, or deliberate). More than 1000 blocks pass. At ~8 s/block, 1000 blocks ≈ 2.2 hours. The default `expiry_hours = 12`, so the tx stays in the pool well past 1000 blocks. [6](#0-5) 

3. The tx is eventually evicted by expiry, size-limit eviction, or conflict resolution → `callbacks.call_reject` → `fee_estimator.reject_tx` → `Algorithm::reject_tx` → `drop_tx_inner(count_failure=true)` → `remove_unconfirmed_tx(entry_height, best_height, bucket_index, true)`. [7](#0-6) 

4. Inside `remove_unconfirmed_tx`, `tx_age = best_height - entry_height > 1000`, so `tx_age - 1 >= 1000` → `confirm_blocks_to_failed_txs[1000][bucket_index]` → **panic**. [8](#0-7) 

---

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` write, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
``` [9](#0-8) 

---

### Proof of Concept

```rust
#[test]
fn test_remove_unconfirmed_tx_oob_panic() {
    let stat = TxConfirmStat::default(); // MAX_CONFIRM_BLOCKS = 1000
    let mut stat = stat;
    // Add a tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();
    // Simulate 1001 blocks passing, then reject (count_failure=true)
    // tx_age = 1001, tx_age - 1 = 1000 >= confirm_blocks_to_failed_txs.len() (1000) → panic
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

Running this test will produce: `thread panicked at 'index out of bounds: the len is 1000 but the index is 1000'`.

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
