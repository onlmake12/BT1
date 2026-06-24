Audit Report

## Title
Out-of-bounds index panic in `TxConfirmStat::remove_unconfirmed_tx` when `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary
`confirm_blocks_to_failed_txs` is initialized with length `MAX_CONFIRM_BLOCKS = 1000`, but `remove_unconfirmed_tx` accesses `confirm_blocks_to_failed_txs[tx_age - 1]` without a bounds check. When a tracked transaction is rejected after more than 1000 blocks in the pool, `tx_age - 1 >= 1000` produces an out-of-bounds index into a `Vec` of length 1000, causing an unconditional Rust panic and crashing the node.

## Finding Description
`confirm_blocks_to_failed_txs` is initialized with exactly `MAX_CONFIRM_BLOCKS = 1000` rows (valid indices 0–999): [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 correctly routes the `block_unconfirmed_txs` access when `tx_age >= self.block_unconfirmed_txs.len()` (i.e., `>= 1000`), but the `confirm_blocks_to_failed_txs` write at line 215 is **unconditional**: [2](#0-1) 

When `tx_age = 1001`, `tx_age - 1 = 1000` indexes slot 1000 of a `Vec` with length 1000 → **panic**.

The full call chain:
1. `accept_tx` calls `track_tx` with `height = self.current_tip`, which records the tx only when `height == self.best_height`: [3](#0-2) [4](#0-3) 

2. Each `commit_block` call advances `best_height` via `process_block`: [5](#0-4) 

3. After 1001 blocks, `reject_tx` → `drop_tx` (with `count_failure=true`) → `drop_tx_inner` → `remove_unconfirmed_tx(entry_height=H, tip_height=H+1001, ...)`: [6](#0-5) [7](#0-6) 

`reject_tx` is only wired for the `ConfirmationFraction` variant: [8](#0-7) 

## Impact Explanation
Any node running the `ConfirmationFraction` fee estimator will **panic and crash** the moment a tracked transaction is rejected or evicted after more than 1000 blocks in the pool. This is a hard Rust index-out-of-bounds panic with no recovery path. This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
An unprivileged attacker submits a valid low-fee transaction via the standard P2P/RPC tx submission path. If the transaction is never included in a block (e.g., fee too low) and remains in the pool for 1001 blocks (~7.8 hours at 28 s/block), any subsequent eviction (pool size limit, expiry, or manual removal) triggers the panic. No special privileges, keys, or hashpower are required. The condition is straightforwardly achievable on mainnet.

## Recommendation
Add a bounds check before the `confirm_blocks_to_failed_txs` access, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure {
    if let Some(row) = self.confirm_blocks_to_failed_txs.get_mut(tx_age - 1) {
        row[bucket_index] += 1f64;
    }
    // silently drop failure samples older than MAX_CONFIRM_BLOCKS
}
```

## Proof of Concept

```rust
#[test]
fn test_no_panic_on_tx_age_exceeding_max_confirm_blocks() {
    use ckb_types::core::FeeRate;
    let mut stat = TxConfirmStat::default(); // MAX_CONFIRM_BLOCKS = 1000

    // Track a tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

    // Advance tip to height 1001 (tx_age will be 1001)
    for h in 1..=1001u64 {
        stat.move_track_window(h);
        stat.decay();
    }

    // This panics: confirm_blocks_to_failed_txs[1000] on a Vec of length 1000
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

The access at line 215 — `confirm_blocks_to_failed_txs[tx_age - 1]` with `tx_age = 1001` — indexes slot 1000 in a `Vec` of length 1000, producing an unconditional panic. [9](#0-8)

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L379-382)
```rust
        if height <= self.best_height {
            return;
        }
        self.best_height = height;
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L400-403)
```rust
        if height != self.best_height {
            // ignore wrong height txs
            return;
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L469-473)
```rust
    pub fn accept_tx(&mut self, tx_hash: Byte32, info: TxEntryInfo) {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        self.track_tx(tx_hash, fee_rate, self.current_tip)
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
