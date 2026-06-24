Audit Report

## Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age` Exceeds Circular Buffer Size — (File: `util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`TxConfirmStat::remove_unconfirmed_tx` guards the `block_unconfirmed_txs` ring-buffer access with a `tx_age >= len` check but applies no equivalent guard before indexing `confirm_blocks_to_failed_txs[tx_age - 1]`. Both arrays have length 1000 (`MAX_CONFIRM_BLOCKS`). When a tracked transaction is rejected after more than 1000 blocks, `tx_age - 1 >= 1000` is out of bounds and Rust panics unconditionally, crashing the node.

## Finding Description

`confirm_blocks_to_failed_txs` is initialized with length `max_confirm_blocks` (= 1000) at line 140:

```rust
let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
```

In `remove_unconfirmed_tx` (lines 197–217), the ring-buffer access is guarded:

```rust
if tx_age >= self.block_unconfirmed_txs.len() {   // guard present
    self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
} else {
    ...
    self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
}
if count_failure {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;  // NO guard
}
```

When `tx_age > 1000`, `tx_age - 1 >= 1000` is out of bounds for a `Vec` of length 1000. Rust panics in both debug and release builds.

The exploit path is fully reachable:
- `reject_tx` (line 475) calls `drop_tx` (line 428), which calls `drop_tx_inner(tx_hash, true)` — `count_failure = true`.
- `drop_tx_inner` calls `remove_unconfirmed_tx` with `count_failure = true` (lines 416–424).
- The reject callback in `shared_builder.rs` (line 600) calls `fee_estimator.reject_tx(&tx_hash)`.
- `remove_expired` in `tx-pool/src/pool.rs` (lines 271–288) calls `callbacks.call_reject`, which fires the reject callback.

## Impact Explanation

A node running with `fee_estimator.algorithm = "ConfirmationFraction"` panics and crashes when any tracked transaction is rejected after more than 1000 blocks. This is a remotely-triggerable node crash, matching the **High** impact: *Vulnerabilities which could easily crash a CKB node* (10001–15000 points).

## Likelihood Explanation

The `ConfirmationFraction` estimator is opt-in, not the default, which limits exposure to nodes that have explicitly enabled it. Once enabled, the trigger is trivial: submit a low-fee transaction (accepted into the pool and tracked), wait 1000+ blocks (~2.8 hours at 10 s/block), and the node crashes when the 12-hour expiry timer fires `remove_expired` or when pool eviction via `limit_size` occurs. No special privileges are required beyond the ability to call `send_transaction` via RPC. The condition is reliably reproducible and repeatable.

## Recommendation

Add a bounds guard before indexing `confirm_blocks_to_failed_txs`, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This ensures that transactions older than `MAX_CONFIRM_BLOCKS` are silently dropped from the failure-count statistics, consistent with how `old_unconfirmed_txs` handles the same overflow case.

## Proof of Concept

1. Configure the node with `fee_estimator.algorithm = "ConfirmationFraction"` in `ckb.toml`.
2. Submit a transaction with a fee rate just above `min_fee_rate` but low enough to remain unconfirmed. The tx is tracked at height `H` with `best_height = H`.
3. Allow 1001+ blocks to be produced (i.e., `best_height` advances to `H + 1001`).
4. Trigger rejection via either:
   - Waiting for the 12-hour expiry timer (`remove_expired` → `callbacks.call_reject` → `fee_estimator.reject_tx`), or
   - Filling the pool to trigger `limit_size` eviction.
5. `remove_unconfirmed_tx` is called with `tx_age = 1001`, `count_failure = true`.
6. `self.confirm_blocks_to_failed_txs[1000]` is accessed on a `Vec` of length 1000 → **thread panics: index out of bounds: the len is 1000 but the index is 1000**, crashing the node.

A unit test can reproduce this directly by constructing a `TxConfirmStat` with `max_confirm_blocks = 1000`, calling `add_unconfirmed_tx` at height 0, then calling `remove_unconfirmed_tx` with `entry_height = 0`, `tip_height = 1001`, `count_failure = true`, and observing the panic.