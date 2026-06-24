Looking at the actual code to verify each claim in the report.

Audit Report

## Title
Out-of-Bounds Panic in `remove_unconfirmed_tx` via Unbounded `tx_age` Index into `confirm_blocks_to_failed_txs` - (File: `util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

In `TxConfirmStat::remove_unconfirmed_tx`, the write to `confirm_blocks_to_failed_txs[tx_age - 1]` is performed unconditionally after a bounds-guarded branch that only protects `block_unconfirmed_txs`. When a tracked transaction has been in the pool for more than `MAX_CONFIRM_BLOCKS = 1000` blocks and is then dropped (e.g., via RBF eviction), `tx_age - 1 >= 1000` produces an out-of-bounds index into a Vec of length 1000, causing a Rust panic. This panic fires inside a write-lock critical section of the `ConfirmationFraction` fee estimator, crashing or permanently disabling the node's fee estimation subsystem.

## Finding Description

`TxConfirmStat` is initialized with three parallel arrays of outer length `max_confirm_blocks = MAX_CONFIRM_BLOCKS = 1000`:

```rust
// confirmation_fraction.rs L139-141
let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
let confirm_blocks_to_failed_txs    = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
let block_unconfirmed_txs           = vec![vec![0;    buckets.len()]; max_confirm_blocks];
```

`remove_unconfirmed_tx` (L197–217) guards the `block_unconfirmed_txs` decrement with a bounds check (`tx_age >= self.block_unconfirmed_txs.len()`), but the `count_failure` write to `confirm_blocks_to_failed_txs` is **outside** that guard and has no bounds check:

```rust
// L208-216
if tx_age >= self.block_unconfirmed_txs.len() {   // len() == 1000
    self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
} else {
    let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
    self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
}
if count_failure {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64; // ← no bounds check
}
```

The call chain that sets `count_failure = true`:

- `reject_tx` (L475) → `drop_tx` (L428) → `drop_tx_inner(tx_hash, true)` (L416) → `remove_unconfirmed_tx(tx_record.height, self.best_height, ..., true)` (L418–424).

`tx_age = self.best_height - tx_record.height`. A transaction tracked at height H with `best_height = H + 1001` yields `tx_age = 1001`, so `tx_age - 1 = 1000`, which is one past the end of a length-1000 Vec — Rust panics unconditionally.

The existing guard (`tx_age >= block_unconfirmed_txs.len()`) is insufficient because it only protects the ring-buffer decrement; it does not gate the `confirm_blocks_to_failed_txs` write.

`reject_tx` is only wired for `ConfirmationFraction` (not `WeightUnitsFlow` or `Dummy`), as confirmed in `estimator/mod.rs` L84–88:

```rust
pub fn reject_tx(&self, tx_hash: &Byte32) {
    match self {
        Self::Dummy | Self::WeightUnitsFlow(_) => {}
        Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
    }
}
```

The panic fires while holding the `RwLock` write guard. Depending on the `RwLock` implementation (`ckb_util::RwLock`), this either poisons the lock (making all future fee-estimator calls fail) or propagates the panic to the calling thread, crashing the node.

## Impact Explanation

**High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

The panic is triggered from `reject_tx`, which is called by the tx-pool service on every transaction eviction. A panic inside a write-lock critical section either crashes the thread (and the node, if it is a non-recoverable service thread) or permanently poisons the `RwLock`, making every subsequent `commit_block` and `estimate_fee_rate` call fail. Both outcomes constitute a node crash or permanent denial-of-service of a core subsystem, matching the High bounty impact class.

## Likelihood Explanation

The `ConfirmationFraction` algorithm must be explicitly configured (`fee_estimator.algorithm = "ConfirmationFraction"` in the node config). Once configured, the attack requires no privilege beyond the public `send_transaction` RPC:

1. Submit a transaction with a fee rate just above `min_fee_rate` (guaranteed to sit in the pool without being mined).
2. Wait for 1001+ blocks (~2.2 hours at 8 s/block).
3. Submit a conflicting RBF transaction to evict the original, or wait for pool-capacity eviction.

No hashpower, Sybil capability, or special access is required. The attack is repeatable and deterministic. Likelihood is **Medium** given the configuration precondition, but trivially executable once that precondition is met.

## Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` write in `remove_unconfirmed_tx`:

```rust
if count_failure {
    let fail_index = tx_age.saturating_sub(1);
    if fail_index < self.confirm_blocks_to_failed_txs.len() {
        self.confirm_blocks_to_failed_txs[fail_index][bucket_index] += 1f64;
    }
}
```

This mirrors the existing guard on `block_unconfirmed_txs` and silently discards failure samples for transactions older than `MAX_CONFIRM_BLOCKS`, consistent with the `old_unconfirmed_txs` overflow path. Additionally, the `TODO` at L247 regarding decay of `old_unconfirmed_txs` should be resolved to prevent denominator inflation in `estimate_median`.

## Proof of Concept

1. Configure the node with `fee_estimator.algorithm = "ConfirmationFraction"`.
2. At block height H, call `send_transaction` with a transaction T at fee rate just above `min_fee_rate`. The estimator calls `accept_tx` → `track_tx` → `add_unconfirmed_tx`, recording T at height H (requires `height == best_height`).
3. Allow 1001 blocks to be committed. Each block calls `commit_block` → `process_block` → `move_track_window` + `decay`. At block H+1000, `move_track_window` moves T's slot into `old_unconfirmed_txs`. `best_height` is now H+1001.
4. Submit a conflicting RBF transaction to evict T. The tx-pool calls `fee_estimator.reject_tx(&T.hash)`.
5. `reject_tx` → `drop_tx` → `drop_tx_inner(T.hash, true)` → `remove_unconfirmed_tx(H, H+1001, bucket_index, true)`.
6. `tx_age = 1001 >= 1000` → enters `old_unconfirmed_txs` branch (correct), then executes `confirm_blocks_to_failed_txs[1000][bucket_index] += 1f64`.
7. `confirm_blocks_to_failed_txs.len() == 1000`; index 1000 is out of bounds → **Rust panic**.

A unit test can reproduce this directly by constructing a `TxConfirmStat` with `max_confirm_blocks = 1000`, calling `add_unconfirmed_tx(0, fee_rate)`, then `remove_unconfirmed_tx(0, 1001, bucket_index, true)` and observing the panic.