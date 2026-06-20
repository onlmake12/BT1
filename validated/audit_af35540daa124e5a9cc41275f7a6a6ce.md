### Title
Unbounded `tx_age` Used as Array Index in Fee Estimator Causes Out-of-Bounds Panic — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

In `confirmation_fraction.rs`, the `remove_unconfirmed_tx` function computes `tx_age = tip_height - entry_height` without capping it to `MAX_CONFIRM_BLOCKS (1000)`. When `count_failure = true` and `tx_age > 1000`, the code accesses `confirm_blocks_to_failed_txs[tx_age - 1]`, which is an out-of-bounds index on a fixed-length array of size 1000. In Rust, this is an unconditional panic in both debug and release builds. A secondary issue exists on the same code path: the plain `-= 1` subtractions on `usize` fields `old_unconfirmed_txs` and `block_unconfirmed_txs[block_index][bucket_index]` (lines 209 and 212) have no bounds check, causing wrapping underflow in release mode if the counter is already zero.

---

### Finding Description

`TxConfirmStat::remove_unconfirmed_tx` is called whenever a tracked transaction is removed from the fee estimator's state. The function computes:

```rust
let tx_age = tip_height.saturating_sub(entry_height) as usize;
```

`tx_age` is unbounded — it equals the number of blocks elapsed since the transaction entered the pool. The function then unconditionally uses it as an array index when `count_failure = true`:

```rust
if count_failure {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

`confirm_blocks_to_failed_txs` is initialized with length `MAX_CONFIRM_BLOCKS = 1000`. Valid indices are `0..=999`. If `tx_age > 1000`, then `tx_age - 1 >= 1000` is out of bounds, causing a panic.

The `count_failure = true` path is reached via `drop_tx` → `drop_tx_inner(tx_hash, true)`, which is called from `reject_tx`. The `reject_tx` function is called from the production tx-pool service (`tx-pool/src/service.rs`, `tx-pool/src/process.rs`) when a transaction is evicted from the pool.

The secondary issue: lines 209 and 212 use plain `-= 1` on `usize` fields:

```rust
self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;   // line 209
self.block_unconfirmed_txs[block_index][bucket_index] -= 1; // line 212
```

In Rust release mode, `usize` subtraction wraps on underflow (to `usize::MAX`), silently corrupting the fee estimator's internal state. The `decay` function explicitly does not decay `old_unconfirmed_txs` (there is a `TODO` comment at line 247 acknowledging this), meaning this counter accumulates without bound across blocks — directly analogous to the Derby `totalWithdrawalRequests` accumulation pattern. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

When a transaction that has been tracked by the fee estimator is evicted from the pool after more than 1000 blocks, the node panics unconditionally. In Rust, out-of-bounds slice indexing panics in both debug and release builds. If the panic occurs in the tx-pool service thread, it crashes that thread or the entire node process, depending on the panic handler configuration. This disrupts transaction processing and fee estimation for all users of the node.

The secondary `usize` underflow silently corrupts the fee estimator's bucket statistics, causing incorrect fee rate estimates to be returned to callers of `estimate_fee_rate` RPC.

---

### Likelihood Explanation

The attack is straightforward and requires no special privileges:

1. An unprivileged tx-pool submitter sends a transaction with a fee rate just above the pool's minimum acceptance threshold.
2. The fee estimator tracks it via `accept_tx` → `track_tx` → `add_unconfirmed_tx`.
3. The transaction remains in the pool for more than 1000 blocks (approximately 2.8 hours at 10-second block times). Low-fee transactions routinely remain unconfirmed for extended periods.
4. The transaction is eventually evicted (pool size limit reached, RBF replacement, or explicit removal).
5. `reject_tx` → `drop_tx` → `drop_tx_inner(tx_hash, true)` → `remove_unconfirmed_tx(..., count_failure=true)` is called.
6. `tx_age = best_height - entry_height > 1000` → `confirm_blocks_to_failed_txs[tx_age - 1]` panics. [3](#0-2) [4](#0-3) 

---

### Recommendation

1. **Cap `tx_age` before using it as an array index.** In `remove_unconfirmed_tx`, clamp `tx_age` to `self.confirm_blocks_to_failed_txs.len()` before the `count_failure` branch:

```rust
if count_failure {
    let index = (tx_age - 1).min(self.confirm_blocks_to_failed_txs.len() - 1);
    self.confirm_blocks_to_failed_txs[index][bucket_index] += 1f64;
}
```

2. **Replace plain `-= 1` with `saturating_sub(1)`.** Lines 209 and 212 should use `saturating_sub` to prevent wrapping underflow:

```rust
self.bucket_stats[bucket_index].old_unconfirmed_txs =
    self.bucket_stats[bucket_index].old_unconfirmed_txs.saturating_sub(1);

self.block_unconfirmed_txs[block_index][bucket_index] =
    self.block_unconfirmed_txs[block_index][bucket_index].saturating_sub(1);
```

3. **Apply decay to `old_unconfirmed_txs`.** The existing `TODO` comment at line 247 should be resolved: `old_unconfirmed_txs` should be decayed alongside `txs_count` to prevent unbounded accumulation. [5](#0-4) 

---

### Proof of Concept

1. Node starts with fee estimator enabled (`is_ready = true`).
2. At block height `H`, a transaction `T` with a low fee rate is submitted to the pool. `accept_tx` is called → `track_tx` stores `T` in `tracked_txs` with `height = H`.
3. `add_unconfirmed_tx(H, fee_rate)` increments `block_unconfirmed_txs[H % 1000][bucket_index]`.
4. Blocks advance. At height `H + 1001`, the pool evicts `T` due to a size limit or RBF.
5. `reject_tx(&T.hash())` → `drop_tx(&T.hash())` → `drop_tx_inner(&T.hash(), true)`.
6. `tracked_txs.remove(&T.hash())` returns `Some(TxRecord { height: H, bucket_index, ... })`.
7. `remove_unconfirmed_tx(H, H + 1001, bucket_index, true)` is called.
8. `tx_age = (H + 1001) - H = 1001`.
9. `tx_age >= 1000` → `old_unconfirmed_txs -= 1` (line 209, secondary issue).
10. `count_failure = true` → `confirm_blocks_to_failed_txs[1001 - 1][bucket_index]` → index `1000` on a length-1000 array → **panic: index out of bounds**. [6](#0-5) [1](#0-0)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L112-156)
```rust
impl Default for TxConfirmStat {
    fn default() -> Self {
        let min_bucket_feerate = f64::from(constants::LOWEST_FEE_RATE.as_u64() as u32);
        // MULTIPLE = max_bucket_feerate / min_bucket_feerate
        const MULTIPLE: f64 = 10000.0;
        let max_bucket_feerate = min_bucket_feerate * MULTIPLE;
        // expect 200 buckets
        let fee_spacing = (MULTIPLE.ln() / 200.0f64).exp();
        // half life each 100 blocks, math.exp(math.log(0.5) / 100)
        let decay_factor: f64 = (0.5f64.ln() / 100.0).exp();

        let mut buckets = Vec::new();
        let mut bucket_fee_boundary = min_bucket_feerate;
        // initialize fee_rate buckets
        while bucket_fee_boundary <= max_bucket_feerate {
            buckets.push(FeeRate::from_u64(bucket_fee_boundary as u64));
            bucket_fee_boundary *= fee_spacing;
        }
        Self::new(buckets, MAX_CONFIRM_BLOCKS, decay_factor)
    }
}

impl TxConfirmStat {
    fn new(buckets: Vec<FeeRate>, max_confirm_blocks: usize, decay_factor: f64) -> Self {
        // max_confirm_blocsk: The number of blocks that the estimator will trace the statistics.
        let min_fee_rate = buckets[0];
        let bucket_stats = vec![BucketStat::default(); buckets.len()];
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
        let fee_rate_to_bucket = buckets
            .into_iter()
            .enumerate()
            .map(|(i, fee_rate)| (fee_rate, i))
            .collect();
        TxConfirmStat {
            min_fee_rate,
            bucket_stats,
            fee_rate_to_bucket,
            block_unconfirmed_txs,
            confirm_blocks_to_confirmed_txs,
            confirm_blocks_to_failed_txs,
            decay_factor,
        }
    }
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L363-414)
```rust
    fn process_block_tx(&mut self, height: u64, tx_hash: &Byte32) -> bool {
        if let Some(tx) = self.drop_tx_inner(tx_hash, false) {
            let blocks_to_confirm = height.saturating_sub(tx.height) as usize;
            self.tx_confirm_stat
                .add_confirmed_tx(blocks_to_confirm, tx.fee_rate);
            true
        } else {
            // tx is not tracked
            false
        }
    }

    /// process new block
    /// record confirm blocks for txs which we tracked before.
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
