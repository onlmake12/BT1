Audit Report

## Title
Index-Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS = 1000` entries. Inside `remove_unconfirmed_tx`, the guard on `block_unconfirmed_txs` correctly handles `tx_age >= 1000`, but the subsequent `confirm_blocks_to_failed_txs[tx_age - 1]` access at line 215 has no corresponding bounds check. When a tracked transaction is rejected after more than 1000 blocks, `tx_age - 1 >= 1000` produces an index-out-of-bounds panic in Rust.

## Finding Description

`confirm_blocks_to_failed_txs` is sized to `max_confirm_blocks = 1000` (valid indices `0..=999`): [1](#0-0) 

Inside `remove_unconfirmed_tx`, the branch at line 208 correctly routes `tx_age >= 1000` to decrement `old_unconfirmed_txs` instead of indexing `block_unconfirmed_txs`: [2](#0-1) 

However, immediately after, the `count_failure` branch unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` with no bounds check: [3](#0-2) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is out of bounds for the 1000-element `Vec`, causing a Rust panic.

**Exploit path:**

1. Attacker submits a low-fee-rate tx. `accept_tx` → `track_tx` records it at `entry_height = H` (requires `height == best_height`): [4](#0-3) 

2. 1001 blocks are committed. Each `commit_block` call advances `best_height` via `process_block`. The tx is never included (low fee), so it remains in `tracked_txs`: [5](#0-4) 

3. The tx-pool's expiry mechanism calls `reject_tx(tx_hash)` → `drop_tx` → `drop_tx_inner(tx_hash, true)` → `remove_unconfirmed_tx(H, H+1001, bucket_index, true)`: [6](#0-5) 

4. `tx_age = 1001`, `count_failure = true` → `confirm_blocks_to_failed_txs[1000]` → **index out of bounds panic**.

The existing guard at line 208 (`tx_age >= self.block_unconfirmed_txs.len()`) is insufficient because it only protects the `block_unconfirmed_txs` access; there is no analogous guard before the `confirm_blocks_to_failed_txs` access.

## Impact Explanation

The panic propagates up through `algo.write().reject_tx(tx_hash)`: [7](#0-6) 

If CKB is compiled with `panic = abort` (common in production), the entire node process terminates — matching **High: Vulnerabilities which could easily crash a CKB node**. Even without `abort`, the panic crashes the tx-pool service task, disabling transaction acceptance and relay for the node.

## Likelihood Explanation

At CKB's ~10-second block time, 1001 blocks ≈ 2.8 hours. Any unprivileged user can submit a low-fee-rate transaction that will not be confirmed, wait for the pool's expiry eviction to fire (which calls `reject_tx`), and trigger the panic. No special privilege, key, or coordination is required. The condition is passively reachable and repeatable.

## Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` access, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This silently discards failure samples for transactions older than `MAX_CONFIRM_BLOCKS`, which is semantically correct since they are already accounted for as `old_unconfirmed_txs`. [8](#0-7) 

## Proof of Concept

```rust
#[test]
fn test_no_panic_on_tx_age_exceeding_max_confirm_blocks() {
    use ckb_types::core::FeeRate;
    // Build a TxConfirmStat with max_confirm_blocks = 1000
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let mut stat = TxConfirmStat::new(buckets, 1000, 0.993f64);

    // Add an unconfirmed tx at entry_height = 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

    // Simulate 1001 blocks passing: move_track_window 1001 times
    for h in 1u64..=1001 {
        stat.move_track_window(h);
    }

    // tx_age = 1001 - 0 = 1001, count_failure = true
    // Without fix: panics at confirm_blocks_to_failed_txs[1000]
    // With fix: returns cleanly
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

This test directly exercises the out-of-bounds access and will panic on the unpatched code, passing after the bounds check is added.

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L140-140)
```rust
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L395-413)
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L463-467)
```rust
    pub fn commit_block(&mut self, block: &BlockView) {
        let tip_number = block.number();
        self.current_tip = tip_number;
        self.process_block(tip_number, block.tx_hashes().iter().map(ToOwned::to_owned));
    }
```

**File:** util/fee-estimator/src/estimator/mod.rs (L84-88)
```rust
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
```
