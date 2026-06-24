The code is confirmed. Let me verify the exact behavior at the boundary.

At line 208, `self.block_unconfirmed_txs.len()` = `MAX_CONFIRM_BLOCKS` = 1000. The guard fires when `tx_age >= 1000`. At `tx_age = 1000`, `tx_age - 1 = 999` — still valid. At `tx_age = 1001`, `tx_age - 1 = 1000` — out of bounds. The guard on line 208 does not protect the access on line 215.

The exploit path is fully confirmed in the code: `reject_tx` (line 475) → `drop_tx` (line 428-430) → `drop_tx_inner(tx_hash, true)` (line 416) → `remove_unconfirmed_tx(..., count_failure=true)` (line 418) → unguarded `confirm_blocks_to_failed_txs[tx_age - 1]` (line 215).

---

Audit Report

## Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary
`confirm_blocks_to_failed_txs` is allocated with length `MAX_CONFIRM_BLOCKS = 1000` (valid indices `0..=999`). In `remove_unconfirmed_tx`, the existing guard at line 208 protects only the `block_unconfirmed_txs` access; the subsequent `confirm_blocks_to_failed_txs[tx_age - 1]` indexing at line 215 has no equivalent guard. When a tracked transaction is evicted after more than 1000 blocks, `tx_age - 1 >= 1000` exceeds the array length, causing a Rust index-out-of-bounds panic that crashes the node process.

## Finding Description
All three tracking arrays are allocated with the same length `max_confirm_blocks = 1000`:

```rust
// lines 139-141
let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
let confirm_blocks_to_failed_txs    = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
let block_unconfirmed_txs           = vec![vec![0;   buckets.len()]; max_confirm_blocks];
```

In `remove_unconfirmed_tx` (lines 197–217):

```rust
let tx_age = tip_height.saturating_sub(entry_height) as usize;
if tx_age < 1 { return; }
if tx_age >= self.block_unconfirmed_txs.len() {   // guard: fires when tx_age >= 1000
    self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
} else {
    let block_index = ...;
    self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
}
if count_failure {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64; // NO GUARD
}
```

When `tx_age = 1001`, `tx_age - 1 = 1000` is out of bounds for a slice of length 1000. The guard on line 208 does not protect the access on line 215.

The full call chain is confirmed:
- `fee_estimator.reject_tx(&tx_hash)` is called from the registered reject callback for every tx eviction (pool size limit, time expiry, conflict) — `shared_builder.rs` line 600.
- `reject_tx` → `drop_tx` (line 428) → `drop_tx_inner(tx_hash, true)` (line 416) → `remove_unconfirmed_tx(..., count_failure=true)` (line 418) → unguarded index at line 215.
- A tx is tracked at `current_tip` (= `best_height`) when it enters the pool (line 472). After 1001 blocks, `best_height - entry_height = 1001`, triggering the panic on eviction.

## Impact Explanation
The panic unwinds the node process. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**. The crash is deterministic and repeatable: any tx that remains unconfirmed for more than 1000 blocks and is then evicted will trigger it.

## Likelihood Explanation
**Limiting factor — opt-in only.** The `ConfirmationFraction` estimator is not the default; nodes must explicitly set `fee_estimator.algorithm = "ConfirmationFraction"` in their config. For nodes that have enabled it, no attacker action beyond submitting a valid low-fee-rate transaction is required. After ~2.8 hours (1001 blocks at ~10 s/block), pool size pressure (`limit_size`) or time expiry (`remove_expired`) will call `reject_tx` and trigger the panic. The default tx-pool expiry is 12 hours, so the trigger window is well within normal operation.

## Recommendation
Add a bounds check before the `confirm_blocks_to_failed_txs` indexing, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

## Proof of Concept
```rust
// In TxConfirmStat unit test (mirrors existing test structure in the file):
let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
let decay = 0.993f64;
let mut stat = TxConfirmStat::new(buckets, 1000, decay);

// Simulate: tx entered at height 0, tip is now 1001 → tx_age = 1001
// bucket_index = 0, count_failure = true (eviction path)
stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000));

// Panics: index out of bounds: the len is 1000 but the index is 1000
stat.remove_unconfirmed_tx(0, 1001, 0, true);
```

`tx_age - 1 = 1000` exceeds `confirm_blocks_to_failed_txs.len() = 1000` (valid range `0..=999`), producing `thread panicked at 'index out of bounds: the len is 1000 but the index is 1000'`.