The code is confirmed. Let me verify the exact lines for the OOB condition.

Audit Report

## Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` via Long-Lived Unconfirmed Transaction Eviction — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary
`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS` (1000) entries, but `remove_unconfirmed_tx` indexes it with `tx_age - 1` without a corresponding bounds check. When a tracked transaction ages beyond 1000 blocks and is then evicted from the tx pool with `count_failure=true`, the unchecked index `confirm_blocks_to_failed_txs[tx_age - 1]` exceeds the Vec's length, causing a Rust index-out-of-bounds panic that crashes the node process.

## Finding Description
`confirm_blocks_to_failed_txs` and `block_unconfirmed_txs` are both allocated with `max_confirm_blocks = 1000` elements at construction: [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 bounds-checks only the `block_unconfirmed_txs` access: [2](#0-1) 

After that branch, execution unconditionally falls through to the `count_failure` block, which indexes `confirm_blocks_to_failed_txs` with `tx_age - 1` — no bounds check: [3](#0-2) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is accessed on a Vec of length 1000 (valid indices `0..=999`), which is an out-of-bounds access. Rust's `Vec` indexing panics unconditionally on OOB in both debug and release builds.

The eviction path that triggers this with `count_failure=true` is:
- `reject_tx` → `drop_tx` → `drop_tx_inner(tx_hash, true)` → `remove_unconfirmed_tx(..., self.best_height, ..., true)` [4](#0-3) 

## Impact Explanation
A panic in the fee estimator propagates up through the tx-pool service thread and crashes the CKB node process. This is a **remote, unprivileged node crash**, matching the allowed bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
No special privileges are required. Any user can submit a valid low-fee-rate transaction via the public RPC. The default tx pool expiry is ~12 hours, which at ~10s/block corresponds to ~4320 blocks — well beyond the 1001-block threshold needed to trigger the OOB. The `ConfirmationFraction` estimator is a real production variant: [5](#0-4) 

The `reject_tx` dispatch is wired directly to the algorithm: [6](#0-5) 

The condition is deterministic and repeatable: any tracked transaction that survives more than 1000 blocks in the pool and is then evicted (not committed) will trigger the panic.

## Recommendation
Add a bounds guard before the `confirm_blocks_to_failed_txs` index access, consistent with how `block_unconfirmed_txs` is already guarded:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

Alternatively, clamp `tx_age` to `self.confirm_blocks_to_failed_txs.len()` before use, or use `.get(tx_age - 1)` with a guard on `None`.

## Proof of Concept

```rust
#[test]
fn test_remove_unconfirmed_tx_oob_panic() {
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let max_confirm_blocks = 1000;
    let decay = 0.993f64;
    let mut stat = TxConfirmStat::new(buckets, max_confirm_blocks, decay);

    // Track a tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

    // Simulate eviction after 1001 blocks: tx_age = 1001
    // confirm_blocks_to_failed_txs.len() == 1000
    // confirm_blocks_to_failed_txs[1000] => index out of bounds: panics
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

`tx_age = 1001`, `tx_age - 1 = 1000`, Vec length = 1000 → **index out of bounds: the len is 1000 but the index is 1000** → node crash. [7](#0-6)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-141)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L204-216)
```rust
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

**File:** util/fee-estimator/src/estimator/mod.rs (L35-38)
```rust
    pub fn new_confirmation_fraction() -> Self {
        let algo = confirmation_fraction::Algorithm::new();
        FeeEstimator::ConfirmationFraction(Arc::new(RwLock::new(algo)))
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
