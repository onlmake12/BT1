The code confirms the vulnerability. Let me verify the key details:

**Line 140** — `confirm_blocks_to_failed_txs` is sized to `max_confirm_blocks = 1000`, valid indices `0..=999`. [1](#0-0) 

**Lines 208–215** — The guard at line 208 protects only `block_unconfirmed_txs`. The `confirm_blocks_to_failed_txs[tx_age - 1]` access at line 215 has no corresponding bounds check. [2](#0-1) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is out of bounds for the 1000-element Vec. The exploit path is fully reachable: `accept_tx` tracks at `current_tip`, 1001 `commit_block` calls advance `best_height`, then `reject_tx` → `drop_tx` → `drop_tx_inner(tx_hash, true)` → `remove_unconfirmed_tx(..., true)` panics. [3](#0-2) [4](#0-3) 

---

Audit Report

## Title
Index-Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS = 1000` entries (valid indices `0..=999`). In `remove_unconfirmed_tx`, the guard at line 208 protects only the `block_unconfirmed_txs` access; the subsequent `confirm_blocks_to_failed_txs[tx_age - 1]` at line 215 has no bounds check. When a tracked transaction is rejected after more than 1000 blocks, `tx_age - 1 >= 1000` produces an index-out-of-bounds panic, crashing the node.

## Finding Description

`confirm_blocks_to_failed_txs` is sized to `max_confirm_blocks = 1000`:

```rust
// L140
let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
```

In `remove_unconfirmed_tx` (L197–L217), the guard at L208 routes `tx_age >= 1000` to decrement `old_unconfirmed_txs` instead of indexing `block_unconfirmed_txs`. However, the `count_failure` branch immediately after unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` with no bounds check:

```rust
if tx_age >= self.block_unconfirmed_txs.len() {   // L208: protects block_unconfirmed_txs only
    self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
} else {
    ...
}
if count_failure {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;  // L215: no guard
}
```

When `tx_age = 1001`, `tx_age - 1 = 1000` is out of bounds for the 1000-element `Vec`, causing a Rust panic.

**Exploit path:**

1. Attacker submits a low-fee-rate tx. `accept_tx` → `track_tx` records it at `entry_height = H` (requires `height == best_height`, satisfied since `accept_tx` passes `self.current_tip`).
2. 1001 blocks are committed. Each `commit_block` call advances `best_height` via `process_block`. The tx is never included (low fee), so it remains in `tracked_txs`.
3. The tx-pool's expiry mechanism calls `reject_tx(tx_hash)` → `drop_tx` → `drop_tx_inner(tx_hash, true)` → `remove_unconfirmed_tx(H, H+1001, bucket_index, true)`.
4. `tx_age = 1001`, `count_failure = true` → `confirm_blocks_to_failed_txs[1000]` → **index out of bounds panic**.

The existing guard at L208 (`tx_age >= self.block_unconfirmed_txs.len()`) is insufficient because it only protects the `block_unconfirmed_txs` access; there is no analogous guard before the `confirm_blocks_to_failed_txs` access.

## Impact Explanation

The panic propagates up through `algo.write().reject_tx(tx_hash)` with no catch. If CKB is compiled with `panic = abort` (common in production), the entire node process terminates. Even without `abort`, the panic crashes the tx-pool service task, disabling transaction acceptance and relay for the node. This matches **High: Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

At CKB's ~10-second block time, 1001 blocks ≈ 2.8 hours. Any unprivileged user can submit a low-fee-rate transaction that will not be confirmed, wait for the pool's expiry eviction to fire (which calls `reject_tx`), and trigger the panic. No special privilege, key, or coordination is required. The condition is passively reachable and repeatable.

## Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` access, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This silently discards failure samples for transactions older than `MAX_CONFIRM_BLOCKS`, which is semantically correct since they are already accounted for as `old_unconfirmed_txs`.

## Proof of Concept

```rust
#[test]
fn test_no_panic_on_tx_age_exceeding_max_confirm_blocks() {
    use ckb_types::core::FeeRate;
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let mut stat = TxConfirmStat::new(buckets, 1000, 0.993f64);

    // Add an unconfirmed tx at entry_height = 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

    // Simulate 1001 blocks passing
    for h in 1u64..=1001 {
        stat.move_track_window(h);
    }

    // tx_age = 1001, count_failure = true
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L208-215)
```rust
        if tx_age >= self.block_unconfirmed_txs.len() {
            self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
        } else {
            let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
            self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
        }
        if count_failure {
            self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
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

**File:** util/fee-estimator/src/estimator/mod.rs (L84-88)
```rust
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
```
