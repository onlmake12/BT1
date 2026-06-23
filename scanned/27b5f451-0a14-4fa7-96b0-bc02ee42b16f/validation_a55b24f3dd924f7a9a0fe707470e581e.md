### Title
Out-of-Bounds Index Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

### Summary

A missing bounds check at line 215 of `confirmation_fraction.rs` allows `confirm_blocks_to_failed_txs[tx_age - 1]` to be accessed with an out-of-bounds index when a tracked transaction has been in the pool for more than `MAX_CONFIRM_BLOCKS` (1000) blocks and is then evicted. This causes a Rust index-out-of-bounds panic, crashing the node process when the `ConfirmationFraction` fee estimator is active.

### Finding Description

In `TxConfirmStat::remove_unconfirmed_tx`, the code correctly guards the `block_unconfirmed_txs` array access against `tx_age >= 1000`: [1](#0-0) 

The guard at line 208 (`if tx_age >= self.block_unconfirmed_txs.len()`) routes old transactions to the `old_unconfirmed_txs` counter — but **no equivalent guard exists** before the `confirm_blocks_to_failed_txs[tx_age - 1]` access at line 215.

`confirm_blocks_to_failed_txs` is initialized with exactly `MAX_CONFIRM_BLOCKS = 1000` entries: [2](#0-1) [3](#0-2) 

Valid indices are `0..=999`. When `tx_age == 1001`, `tx_age - 1 == 1000` is out of bounds → Rust panics.

### Impact Explanation

The panic propagates up through the call chain:

`reject_tx` → `drop_tx` (with `count_failure=true`) → `drop_tx_inner` → `remove_unconfirmed_tx` [4](#0-3) [5](#0-4) [6](#0-5) 

The `FeeEstimator::reject_tx` dispatcher confirms this only fires for the `ConfirmationFraction` variant: [7](#0-6) 

A Rust index-out-of-bounds panic in a synchronous write-lock context will terminate the thread holding the lock, crashing the node.

### Likelihood Explanation

The preconditions are realistic:
1. **Attacker submits a low-fee transaction** via RPC `send_transaction` or P2P relay — unprivileged.
2. **Transaction is tracked** by the estimator: `track_tx` only records a tx if `height == best_height` at submission time, which is the normal case for any freshly submitted tx.
3. **Chain advances >1000 blocks** without the tx being confirmed (low fee rate, or deliberate fee underpricing).
4. **Eviction is triggered**: `limit_size` or RBF replacement calls `callbacks.call_reject` → `fee_estimator.reject_tx`.

The attacker does not need any privileged access. The only constraint is that the `ConfirmationFraction` algorithm must be configured (not `Dummy` or `WeightUnitsFlow`).

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` access, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This is consistent with the intent: transactions older than `MAX_CONFIRM_BLOCKS` are already classified as "old" and their failure should simply be silently dropped (or counted separately), not recorded in the fixed-size array.

### Proof of Concept

```rust
#[test]
fn test_remove_unconfirmed_tx_oob_panic() {
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let mut stat = TxConfirmStat::new(buckets, 1000, 0.993);

    // Track a tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

    // Simulate chain advancing 1001 blocks, then eviction with count_failure=true
    // tx_age = 1001 - 0 = 1001 > 1000 = confirm_blocks_to_failed_txs.len()
    // confirm_blocks_to_failed_txs[1001 - 1] = confirm_blocks_to_failed_txs[1000] → PANIC
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

Running this test against the unpatched code panics with:
```
thread 'test' panicked at 'index out of bounds: the len is 1000 but the index is 1000'
``` [8](#0-7)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L22-22)
```rust
const MAX_CONFIRM_BLOCKS: usize = 1000;
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-140)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L427-430)
```rust
    /// tx removed from txpool
    fn drop_tx(&mut self, tx_hash: &Byte32) -> bool {
        self.drop_tx_inner(tx_hash, true).is_some()
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L475-477)
```rust
    pub fn reject_tx(&mut self, tx_hash: &Byte32) {
        let _ = self.drop_tx(tx_hash);
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
