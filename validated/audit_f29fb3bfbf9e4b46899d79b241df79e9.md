### Title
Phantom Unconfirmed-Tx Entries Accumulate Without Decay and Trigger Out-of-Bounds Panic on Eviction — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

The `ConfirmationFraction` fee estimator tracks transactions in a fixed-size circular window of `MAX_CONFIRM_BLOCKS = 1000` slots. When a tracked transaction ages past that window it is moved to `old_unconfirmed_txs` but remains in the `tracked_txs` HashMap. The `decay()` function explicitly skips decaying `old_unconfirmed_txs` (marked with a `// TODO`). When such a transaction is eventually evicted from the pool, `remove_unconfirmed_tx` is called with `count_failure = true` and a `tx_age > 1000`. The code then unconditionally indexes `self.confirm_blocks_to_failed_txs[tx_age - 1]`, which has length exactly 1000, causing an out-of-bounds panic and crashing the node.

---

### Finding Description

**File**: `util/fee-estimator/src/estimator/confirmation_fraction.rs`

The `TxConfirmStat` struct maintains:

```
confirm_blocks_to_failed_txs: Vec<Vec<f64>>   // length = MAX_CONFIRM_BLOCKS = 1000
block_unconfirmed_txs:        Vec<Vec<usize>>  // length = MAX_CONFIRM_BLOCKS = 1000
```

When a new block arrives, `move_track_window` rotates the circular buffer and moves the oldest slot's counts into `old_unconfirmed_txs`: [1](#0-0) 

The `decay()` function applies a half-life decay to all statistical counters **except** `old_unconfirmed_txs`, as noted by the explicit TODO comment: [2](#0-1) 

Critically, the `tracked_txs` HashMap is **never pruned** when entries age past the window. When a tx is finally evicted and `reject_tx` → `drop_tx_inner(count_failure=true)` → `remove_unconfirmed_tx` is called: [3](#0-2) 

The `count_failure` branch at line 214–216 is **unconditional** — it executes regardless of whether `tx_age >= block_unconfirmed_txs.len()`. When `tx_age > 1000`, `tx_age - 1 >= 1000`, and `self.confirm_blocks_to_failed_txs[tx_age - 1]` panics with an index-out-of-bounds error.

The `tracked_txs` HashMap retains the entry with its original `entry_height`: [4](#0-3) 

`drop_tx_inner` passes `self.best_height` as `tip_height`, so `tx_age = best_height - entry_height`. Any tx tracked at height H and evicted at height H + 1001 or later triggers the panic. [5](#0-4) 

---

### Impact Explanation

A node running the `ConfirmationFraction` fee estimator crashes with an index-out-of-bounds panic. The panic propagates from the fee estimator write-lock holder through the tx-pool callback chain, terminating the node process. This is a **remote node crash** (denial of service) triggered by an unprivileged transaction sender.

The three eviction paths that all call `callbacks.call_reject` and therefore reach `reject_tx`:

- **Size-limit eviction** (`limit_size`): [6](#0-5) 
- **Expiry eviction** (`remove_expired`): [7](#0-6) 
- **RBF replacement** (`process_rbf`): [8](#0-7) 

The `call_reject` callback dispatches to `fee_estimator.reject_tx`: [9](#0-8) 

---

### Likelihood Explanation

**High**. The attacker only needs to:

1. Submit one transaction with a fee rate just above `min_fee_rate` so it is accepted into the pool and tracked by the estimator.
2. Wait for 1001 blocks (~2.8 hours on mainnet at 10-second block time) without the transaction being confirmed.
3. Trigger eviction — either by submitting a higher-fee conflicting transaction (RBF), by flooding the pool to trigger size-limit eviction, or simply by waiting for the expiry timer.

No special privilege, key, or majority hashpower is required. A single unprivileged RPC caller or P2P transaction relayer can execute this.

---

### Recommendation

Add a bounds check in `remove_unconfirmed_tx` before indexing `confirm_blocks_to_failed_txs`:

```rust
if count_failure {
    let index = tx_age.saturating_sub(1);
    if index < self.confirm_blocks_to_failed_txs.len() {
        self.confirm_blocks_to_failed_txs[index][bucket_index] += 1f64;
    }
}
```

Additionally, resolve the `// TODO` in `decay()` by also applying the decay factor to `old_unconfirmed_txs` to prevent phantom entries from accumulating indefinitely: [10](#0-9) 

---

### Proof of Concept

```
1. Node starts with ConfirmationFraction fee estimator enabled.
2. At tip height H, attacker submits tx T with fee_rate just above min_fee_rate.
   → accept_tx(T.hash, ...) → track_tx(T.hash, fee_rate, H)
   → tracked_txs[T.hash] = TxRecord { height: H, ... }
3. Blocks H+1 … H+1000 are mined. Each call to commit_block:
   → move_track_window moves the slot for height H into old_unconfirmed_txs
   → tracked_txs still contains T.hash with height H
4. At height H+1001, attacker submits a conflicting tx T' with higher fee (RBF).
   → process_rbf removes T from pool
   → callbacks.call_reject(T, RBFRejected)
   → fee_estimator.reject_tx(T.hash)
   → drop_tx_inner(T.hash, count_failure=true)
   → remove_unconfirmed_tx(entry_height=H, tip_height=H+1001, ..., count_failure=true)
   → tx_age = 1001
   → self.confirm_blocks_to_failed_txs[1000]  ← INDEX OUT OF BOUNDS (len=1000)
   → thread panics → node crashes
```

### Citations

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L219-227)
```rust
    fn move_track_window(&mut self, height: u64) {
        let block_index = (height % (self.block_unconfirmed_txs.len() as u64)) as usize;
        for bucket_index in 0..self.bucket_stats.len() {
            // mark unconfirmed txs as old_unconfirmed_txs
            self.bucket_stats[bucket_index].old_unconfirmed_txs +=
                self.block_unconfirmed_txs[block_index][bucket_index];
            self.block_unconfirmed_txs[block_index][bucket_index] = 0;
        }
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L229-249)
```rust
    /// apply decay factor on stats, smoothly reduce the effects of old samples.
    fn decay(&mut self) {
        let decay_factor = self.decay_factor;
        for (bucket_index, bucket) in self.bucket_stats.iter_mut().enumerate() {
            self.confirm_blocks_to_confirmed_txs
                .iter_mut()
                .for_each(|buckets| {
                    buckets[bucket_index] *= decay_factor;
                });

            self.confirm_blocks_to_failed_txs
                .iter_mut()
                .for_each(|buckets| {
                    buckets[bucket_index] *= decay_factor;
                });
            bucket.total_fee_rate =
                FeeRate::from_u64((bucket.total_fee_rate.as_u64() as f64 * decay_factor) as u64);
            bucket.txs_count *= decay_factor;
            // TODO do we need decay the old unconfirmed?
        }
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L394-414)
```rust
    /// track a tx that entered txpool
    fn track_tx(&mut self, tx_hash: Byte32, fee_rate: FeeRate, height: u64) {
        if self.tracked_txs.contains_key(&tx_hash) {
            // already in track
            return;
        }
        if height != self.best_height {
            // ignore wrong height txs
            return;
        }
        if let Some(bucket_index) = self.tx_confirm_stat.add_unconfirmed_tx(height, fee_rate) {
            self.tracked_txs.insert(
                tx_hash,
                TxRecord {
                    height,
                    bucket_index,
                    fee_rate,
                },
            );
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

**File:** tx-pool/src/pool.rs (L271-287)
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
```

**File:** tx-pool/src/pool.rs (L306-324)
```rust
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

**File:** tx-pool/src/callback.rs (L64-69)
```rust
    /// Call on after reject
    pub fn call_reject(&self, tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject) {
        if let Some(call) = &self.reject {
            call(tx_pool, entry, reject)
        }
    }
```
