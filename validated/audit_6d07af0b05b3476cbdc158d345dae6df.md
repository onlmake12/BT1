Audit Report

## Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`remove_unconfirmed_tx` guards the `block_unconfirmed_txs` access with a `tx_age >= len` check but unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without a corresponding bounds check. When a tracked tx ages beyond `MAX_CONFIRM_BLOCKS` (1000) blocks and is then evicted from the tx-pool with `count_failure=true`, `tx_age - 1 >= 1000` produces an index-out-of-bounds Rust panic, crashing the node.

## Finding Description

Both `confirm_blocks_to_failed_txs` and `block_unconfirmed_txs` are allocated with `max_confirm_blocks = 1000` elements: [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 correctly routes `block_unconfirmed_txs` access to `old_unconfirmed_txs` when `tx_age >= 1000`, avoiding OOB there: [2](#0-1) 

However, the `confirm_blocks_to_failed_txs` write at line 215 has **no equivalent bounds check**: [3](#0-2) 

- `tx_age = 1000`: index `999` → valid (last element).
- `tx_age = 1001`: index `1000` → **out of bounds** on a 1000-element `Vec` → Rust panic.

The call chain is: `reject_tx` (line 475) → `drop_tx` (line 428-429, always passes `count_failure=true`) → `drop_tx_inner` (line 416) → `remove_unconfirmed_tx`: [4](#0-3) [5](#0-4) 

## Impact Explanation

A Rust `Vec` index-out-of-bounds is an unrecoverable panic. The caller does not wrap the call in `std::panic::catch_unwind`, so the thread aborts and the node process crashes. This is a remotely triggerable node crash, matching the allowed CKB bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation

An unprivileged attacker only needs to:
1. Submit a valid low-fee-rate tx (accepted into the pool and tracked by the fee estimator via `accept_tx`).
2. Wait for 1001+ blocks to pass (~2.8 hours at 10-second block times) without the tx being mined.
3. Trigger eviction — e.g., flood the pool with higher-fee txs to push the low-fee tx out, or wait for wall-clock expiry.

Step 3 causes `reject_tx` → `remove_unconfirmed_tx` with `tx_age ≥ 1001` and `count_failure=true`, producing the panic. No special privileges are required; any user who can submit a transaction to the mempool can trigger this.

## Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` write, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

Alternatively, use `.get_mut(tx_age - 1)` and silently skip if out of range, consistent with the design intent that txs older than `MAX_CONFIRM_BLOCKS` are not tracked for failure statistics.

## Proof of Concept

```rust
#[test]
fn test_oob_panic_on_old_tx_eviction() {
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let max_confirm_blocks = 1000;
    let decay = 0.993f64;
    let mut stat = TxConfirmStat::new(buckets, max_confirm_blocks, decay);

    // Track a tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

    // Simulate 1001 blocks passing
    for h in 1u64..=1001 {
        stat.move_track_window(h);
    }

    // tx_age = 1001 - 0 = 1001; confirm_blocks_to_failed_txs[1000] → PANIC
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

Running this test panics with `index out of bounds: the len is 1000 but the index is 1000`, confirming the vulnerability.

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-141)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L208-213)
```rust
        if tx_age >= self.block_unconfirmed_txs.len() {
            self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
        } else {
            let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
            self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
        }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L214-216)
```rust
        if count_failure {
            self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
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
