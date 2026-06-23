### Title
Out-of-Bounds Panic in `remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (File: `util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

In the `ConfirmationFraction` fee estimator, `remove_unconfirmed_tx` correctly guards the `block_unconfirmed_txs` array access with a bounds check (`tx_age >= self.block_unconfirmed_txs.len()`), but then unconditionally accesses `confirm_blocks_to_failed_txs[tx_age - 1]` without an equivalent guard. When a tracked transaction is evicted from the tx-pool after more than `MAX_CONFIRM_BLOCKS` (1000) blocks, `tx_age - 1 >= 1000` causes a Rust index-out-of-bounds **panic**, crashing the CKB node process.

---

### Finding Description

`TxConfirmStat` is initialized with both `block_unconfirmed_txs` and `confirm_blocks_to_failed_txs` as vectors of length `MAX_CONFIRM_BLOCKS = 1000`: [1](#0-0) 

The `remove_unconfirmed_tx` function computes `tx_age` as the difference between the current tip height and the height at which the tx was first tracked: [2](#0-1) 

The guard on line 208 (`if tx_age >= self.block_unconfirmed_txs.len()`) correctly routes the `block_unconfirmed_txs` decrement to `old_unconfirmed_txs` when `tx_age >= 1000`. However, the subsequent access on line 215:

```rust
self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
```

has **no corresponding bounds check**. `confirm_blocks_to_failed_txs` also has length 1000 (indices `0..=999`). When `tx_age = 1001`, `tx_age - 1 = 1000` is out of bounds, causing a Rust panic.

The `Algorithm` struct initializes `best_height = 0` and `current_tip = 0`: [3](#0-2) 

Transactions are tracked at the current `best_height` via `track_tx`: [4](#0-3) 

A tx tracked at height `H` and still present in the pool at height `H + 1001` will produce `tx_age = 1001` when evicted, triggering the panic. The `drop_tx` path (called on pool eviction/rejection) passes `count_failure = true`, which is the only branch that reaches the unbounded access: [5](#0-4) 

The `FeeEstimator::reject_tx` dispatcher confirms this path is exclusive to the `ConfirmationFraction` variant and is called on every tx rejection: [6](#0-5) 

---

### Impact Explanation

A CKB node running the `ConfirmationFraction` fee estimator will **panic and crash** when any tracked transaction is evicted from the tx-pool after having been present for more than 1000 blocks. This is a hard process termination (Rust index-out-of-bounds panic), not a graceful error. The node must be restarted manually. If the condition is reproducible (e.g., the attacker re-submits the same low-fee tx after restart), the node can be repeatedly crashed.

---

### Likelihood Explanation

On CKB mainnet, blocks arrive approximately every 8–10 seconds, so 1001 blocks ≈ 2.2–2.8 hours. A transaction with a fee rate just above the node's `min_fee_rate` will be accepted into the pool and will remain there as long as the pool is not full. An attacker can:

1. Submit a tx with a fee rate slightly above `min_fee_rate`.
2. Wait 1001+ blocks.
3. Flood the pool with high-fee transactions to trigger eviction of the low-fee tx.
4. The node panics.

No privileged access, no key material, and no majority hashpower is required. The entry point is the standard `send_transaction` RPC, accessible to any unprivileged tx-pool submitter.

---

### Recommendation

Add a bounds check before accessing `confirm_blocks_to_failed_txs[tx_age - 1]`, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This caps the failure-count recording to the tracked window, consistent with how `block_unconfirmed_txs` handles aged-out transactions via `old_unconfirmed_txs`.

---

### Proof of Concept

```
1. Node is configured with ConfirmationFraction fee estimator and is_ready = true
   (i.e., IBD is complete).

2. Attacker calls send_transaction RPC with a valid tx whose fee_rate is just
   above min_fee_rate. The estimator calls accept_tx → track_tx, recording
   TxRecord { height: H, bucket_index: B, fee_rate: F }.

3. 1001+ blocks pass. best_height advances to H + 1001.

4. Attacker (or natural pool pressure) causes the tx to be evicted:
   FeeEstimator::reject_tx → Algorithm::reject_tx → drop_tx → drop_tx_inner
   → remove_unconfirmed_tx(entry_height=H, tip_height=H+1001,
                            bucket_index=B, count_failure=true)

5. tx_age = (H+1001).saturating_sub(H) = 1001
   tx_age >= block_unconfirmed_txs.len() (1000) → old_unconfirmed_txs branch taken ✓
   count_failure = true →
     self.confirm_blocks_to_failed_txs[1001 - 1][B]  ← index 1000 on a len-1000 Vec
     → thread 'main' panicked at 'index out of bounds: the len is 1000 but the index is 1000'
     → node process terminates
``` [7](#0-6) [8](#0-7) [1](#0-0)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L22-22)
```rust
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L352-361)
```rust
    pub fn new() -> Self {
        Self {
            best_height: 0,
            start_height: 0,
            tx_confirm_stat: Default::default(),
            tracked_txs: Default::default(),
            current_tip: 0,
            is_ready: false,
        }
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L395-414)
```rust
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L427-430)
```rust
    /// tx removed from txpool
    fn drop_tx(&mut self, tx_hash: &Byte32) -> bool {
        self.drop_tx_inner(tx_hash, true).is_some()
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
