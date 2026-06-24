Audit Report

## Title
Out-of-Bounds Index Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`remove_unconfirmed_tx` correctly bounds-checks `block_unconfirmed_txs` when `tx_age >= 1000`, but then unconditionally indexes into `confirm_blocks_to_failed_txs[tx_age - 1]` without any corresponding bounds check. Both vecs have length `MAX_CONFIRM_BLOCKS = 1000`, so when `tx_age >= 1001`, the access at index `tx_age - 1 >= 1000` is out of bounds and Rust panics unconditionally. Any unprivileged user can trigger this by submitting a minimum-fee transaction and waiting for it to be evicted after 1001+ blocks, crashing the node's tx-pool service.

## Finding Description

`confirm_blocks_to_failed_txs` and `block_unconfirmed_txs` are both initialized with length `MAX_CONFIRM_BLOCKS = 1000`: [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 correctly routes to `old_unconfirmed_txs` when `tx_age >= self.block_unconfirmed_txs.len()` (i.e., `tx_age >= 1000`). However, the `count_failure` branch at lines 214–215 uses the same unbounded `tx_age` to index `confirm_blocks_to_failed_txs` with no equivalent guard: [2](#0-1) 

When `tx_age == 1001`, `tx_age - 1 == 1000` is out of bounds for a 1000-element `Vec`. Rust panics unconditionally.

The exploit path:
1. A tx enters the pool at `entry_height = H`; `track_tx` records it only when `height == self.best_height`: [3](#0-2) 
2. 1001+ blocks pass; `best_height` advances to `H + 1001`.
3. Any eviction (time expiry, pool-full, RBF) fires the registered reject callback → `fee_estimator.reject_tx(&tx_hash)` → `drop_tx(count_failure=true)` → `drop_tx_inner` → `remove_unconfirmed_tx(H, H+1001, bucket_index, true)`: [4](#0-3) 
4. `tx_age = 1001`, `confirm_blocks_to_failed_txs[1000]` → **panic**.

The tx pool uses time-based expiry, not block-based, so a tx can trivially remain for 1001+ blocks before eviction.

## Impact Explanation

A Rust index-out-of-bounds panic unwinds through the reject callback. If the callback is invoked while holding the tx-pool write lock (as shown in `shared_builder.rs` lines 576–601), the panic poisons the `RwLock`, causing all subsequent tx-pool operations to panic as well. This effectively crashes the tx-pool service thread and renders the node non-functional. This matches the allowed CKB bounty impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

- The attacker only needs to submit one valid minimum-fee transaction and wait.
- No privileged access, no PoW, no key material required.
- The trigger is automatic: any eviction path (time expiry, pool-full, RBF) calls `reject_tx` with `count_failure=true`.
- At ~10 seconds per CKB block, 1001 blocks ≈ 2.78 hours. Any node with `expiry_hours > ~2.8` (the common default is 12 hours) is vulnerable.
- The condition is deterministic and repeatable.

## Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` access, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This caps failure recording to the tracked window and silently drops samples for txs older than `MAX_CONFIRM_BLOCKS`, which is already the intended semantic (they are already counted as `old_unconfirmed_txs`).

## Proof of Concept

```rust
#[test]
fn test_oob_panic_remove_unconfirmed_tx() {
    let mut stat = TxConfirmStat::default(); // MAX_CONFIRM_BLOCKS = 1000
    let fee_rate = FeeRate::from_u64(1000);
    let entry_height: u64 = 0;
    // Track tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(entry_height, fee_rate).unwrap();
    // Advance tip to height 1001 (tx_age = 1001 > 1000)
    let tip_height: u64 = 1001;
    // This panics: confirm_blocks_to_failed_txs[1000] is OOB for a 1000-element Vec
    stat.remove_unconfirmed_tx(entry_height, tip_height, bucket_index, true);
}
```

This test directly exercises the OOB access at line 215 and will panic with an index-out-of-bounds error in debug and release builds alike. [5](#0-4)

### Citations

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
