Looking at the actual code in `util/fee-estimator/src/estimator/confirmation_fraction.rs`:

**Allocation** — `confirm_blocks_to_failed_txs` is allocated with exactly `max_confirm_blocks` (1000) rows: [1](#0-0) 

**The vulnerable write** — `remove_unconfirmed_tx` computes `tx_age` and guards the `block_unconfirmed_txs` path, but the `count_failure` branch at line 214–215 has no corresponding bound check: [2](#0-1) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is past the end of the 1000-element vector. The guard at line 208 only protects the `block_unconfirmed_txs` decrement — it does not protect the `confirm_blocks_to_failed_txs` write.

**Reachability** — `drop_tx_inner` passes `self.best_height` as `tip_height`, and `track_tx` records `entry_height = self.best_height` at acceptance time: [3](#0-2) [4](#0-3) 

There is no automatic expiry of entries in `tracked_txs`. If `best_height` advances by more than 1000 blocks before `reject_tx` is called, the panic fires unconditionally.

---

Audit Report

## Title
Out-of-Bounds Panic in `remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary
`TxConfirmStat::remove_unconfirmed_tx` indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without bounding `tx_age` against the vector's length of 1000. When a tracked transaction is rejected from the tx pool after more than 1000 blocks have elapsed, `tx_age - 1 >= 1000` causes an unconditional Rust bounds-check panic, crashing the node.

## Finding Description
`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS = 1000` rows (valid indices 0–999) at line 140. In `remove_unconfirmed_tx` (lines 197–217), `tx_age` is computed as `tip_height.saturating_sub(entry_height)`. The guard at line 208 (`if tx_age >= self.block_unconfirmed_txs.len()`) only decides which counter to decrement for the `block_unconfirmed_txs` bookkeeping — it does not bound `tx_age` before the write at line 215:

```rust
self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
```

When `tx_age = 1001`, the index `1000` is out of bounds for a 1000-element vector. Rust's indexing operator panics unconditionally in both debug and release builds.

The call chain is: `reject_tx` → `drop_tx` → `drop_tx_inner` → `remove_unconfirmed_tx` with `count_failure = true` and `tip_height = self.best_height`. A tx is tracked at `entry_height = self.best_height` at the time of `accept_tx`. There is no automatic expiry of entries in `tracked_txs`, so any tx that remains in the pool across 1001+ blocks and is then evicted triggers the panic.

## Impact Explanation
This is a **node crash** — a complete loss of availability for the affected node. Tx pool eviction is a normal operational event (triggered by pool-size limits, RBF replacement, or explicit removal), making this crash reachable under ordinary conditions. This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
An unprivileged attacker needs only two steps: (1) submit a minimum-fee transaction to the pool, and (2) after 1001+ blocks, force its eviction by flooding the pool with higher-fee transactions. No special privilege, no PoW, no key material is required — only the ability to submit transactions via RPC or P2P relay. The scenario also arises naturally without an attacker if a low-fee tx lingers in the pool during a period of low activity and is later evicted.

## Recommendation
Add a bounds check before the `confirm_blocks_to_failed_txs` write in `remove_unconfirmed_tx`, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure {
    let fail_index = tx_age - 1;
    if fail_index < self.confirm_blocks_to_failed_txs.len() {
        self.confirm_blocks_to_failed_txs[fail_index][bucket_index] += 1f64;
    }
}
```

This silently saturates the failure counter at the maximum tracked age, which is the correct semantic: the tx aged out of the tracking window.

## Proof of Concept
```rust
#[test]
fn test_oob_panic_tx_age_gt_max_confirm_blocks() {
    use super::*;
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let max_confirm_blocks = 1000;
    let decay = 0.993f64;
    let mut stat = TxConfirmStat::new(buckets, max_confirm_blocks, decay);

    let entry_height: u64 = 0;
    let fee_rate = FeeRate::from_u64(1500);
    let bucket_index = stat.add_unconfirmed_tx(entry_height, fee_rate).unwrap();

    for h in 1..=1001u64 {
        stat.move_track_window(h);
    }

    // tx_age = 1001 - 0 = 1001; confirm_blocks_to_failed_txs[1000] → OOB panic
    stat.remove_unconfirmed_tx(entry_height, 1001, bucket_index, true);
}
```

Running this test produces:
```
thread 'test_oob_panic_tx_age_gt_max_confirm_blocks' panicked at
'index out of bounds: the len is 1000 but the index is 1000'
util/fee-estimator/src/estimator/confirmation_fraction.rs:215
```

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
