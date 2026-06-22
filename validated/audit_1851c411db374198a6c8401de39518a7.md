### Title
Out-of-Bounds Index Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

### Summary

A missing bounds check on `confirm_blocks_to_failed_txs` in `remove_unconfirmed_tx` allows an index-out-of-bounds panic when a tracked transaction remains unconfirmed for more than `MAX_CONFIRM_BLOCKS` (1000) blocks before being rejected. Any unprivileged submitter can trigger this by submitting a valid minimum-fee transaction and waiting for it to be evicted after 1001+ blocks, crashing the tx-pool service.

### Finding Description

`TxConfirmStat` is initialized with both `confirm_blocks_to_failed_txs` and `block_unconfirmed_txs` as `Vec` of length `MAX_CONFIRM_BLOCKS = 1000`: [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 correctly bounds-checks the `block_unconfirmed_txs` access when `tx_age >= 1000`, routing to the `old_unconfirmed_txs` branch. However, the subsequent `count_failure` branch at line 214–215 uses the **same unbounded `tx_age`** to index into `confirm_blocks_to_failed_txs`, which has the same length of 1000: [2](#0-1) 

When `tx_age == 1001`, `tx_age - 1 == 1000`, which is out of bounds for a 1000-element `Vec`. Rust panics unconditionally on this access.

The call chain is:

1. `accept_tx` → `track_tx` → `add_unconfirmed_tx` records the tx at `entry_height = current_tip` [3](#0-2) 

2. 1001+ blocks pass; `best_height` advances to `entry_height + 1001` [4](#0-3) 

3. Any rejection (time expiry, pool-full eviction, RBF) fires the registered reject callback → `fee_estimator.reject_tx(&tx_hash)` → `drop_tx(count_failure=true)` → `drop_tx_inner` → `remove_unconfirmed_tx(entry_height, best_height=entry_height+1001, bucket_index, true)` [5](#0-4) 

4. `tx_age = 1001`, `confirm_blocks_to_failed_txs[1000]` → **panic** [6](#0-5) 

The tx pool uses **time-based** expiry (hours), not block-based: [7](#0-6) 

At ~10 seconds per CKB block, 1001 blocks ≈ 2.78 hours. If the configured `expiry_hours` exceeds this (e.g., the common default of 12 hours), a tx can trivially remain in the pool long enough to trigger the OOB on eventual eviction.

### Impact Explanation

A Rust index-out-of-bounds panic unwinds through the reject callback, which is called while holding the tx-pool write lock. The panic poisons the `RwLock`, causing all subsequent tx-pool operations to panic as well, effectively crashing the tx-pool service thread and rendering the node non-functional. Nodes running the `ConfirmationFraction` fee estimator that are widely deployed would all be vulnerable to this crash.

### Likelihood Explanation

- The attacker only needs to submit one valid minimum-fee transaction and wait.
- No privileged access, no PoW, no key material required.
- The trigger is automatic: any eviction path (time expiry, pool-full, RBF) calls `reject_tx` with `count_failure=true`.
- The condition (`tx_age > 1000`) is reachable whenever `expiry_hours > ~2.8`.

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` access, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This caps failure recording to the tracked window and silently drops samples for txs older than `MAX_CONFIRM_BLOCKS`, which is already the intended semantic (they are already counted as `old_unconfirmed_txs`).

### Proof of Concept

```rust
#[test]
fn test_oob_panic_remove_unconfirmed_tx() {
    let mut stat = TxConfirmStat::default(); // MAX_CONFIRM_BLOCKS = 1000
    let fee_rate = FeeRate::from_u64(1000);
    let entry_height: u64 = 0;
    // Track tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(entry_height, fee_rate).unwrap();
    // Advance tip to height 1001 (tx_age = 1001 > 1000)
    let tip_height: u64 = 1001;
    // This panics: confirm_blocks_to_failed_txs[1000] is OOB for a 1000-element Vec
    stat.remove_unconfirmed_tx(entry_height, tip_height, bucket_index, true);
}
``` [8](#0-7)

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L377-392)
```rust
    fn process_block(&mut self, height: u64, txs: impl Iterator<Item = Byte32>) {
        // For simpfy, we assume chain reorg will not effect tx fee.
        if height <= self.best_height {
            return;
        }
        self.best_height = height;
        // update tx confirm stat
        self.tx_confirm_stat.move_track_window(height);
        self.tx_confirm_stat.decay();
        let processed_txs = txs.filter(|tx| self.process_block_tx(height, tx)).count();
        if self.start_height == 0 && processed_txs > 0 {
            // start record
            self.start_height = self.best_height;
            ckb_logger::debug!("start recording at {}", self.start_height);
        }
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L416-425)
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
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L469-473)
```rust
    pub fn accept_tx(&mut self, tx_hash: Byte32, info: TxEntryInfo) {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        self.track_tx(tx_hash, fee_rate, self.current_tip)
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

**File:** tx-pool/src/pool.rs (L57-57)
```rust
        let expiry = config.expiry_hours as u64 * 60 * 60 * 1000;
```
