All claims in the report are verified against the actual code. Here is the validated finding:

---

Audit Report

## Title
Out-of-Bounds Panic in `remove_unconfirmed_tx` via Unbounded `tx_age` Index into `confirm_blocks_to_failed_txs` - (File: `util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`TxConfirmStat::remove_unconfirmed_tx` writes to `confirm_blocks_to_failed_txs[tx_age - 1]` without a bounds check. When a tracked transaction has been in the pool for more than `MAX_CONFIRM_BLOCKS = 1000` blocks and is then evicted (e.g., via RBF), `tx_age - 1 >= 1000` produces an out-of-bounds index into a Vec of length 1000, causing a Rust panic inside the `RwLock` write-guard of the `ConfirmationFraction` fee estimator.

## Finding Description

`TxConfirmStat` is initialized with three parallel arrays of outer length `max_confirm_blocks = MAX_CONFIRM_BLOCKS = 1000`: [1](#0-0) [2](#0-1) 

`remove_unconfirmed_tx` guards the `block_unconfirmed_txs` decrement with a bounds check (`tx_age >= self.block_unconfirmed_txs.len()`), but the `count_failure` write to `confirm_blocks_to_failed_txs` is **outside** that guard and has no bounds check: [3](#0-2) 

When `tx_age = 1001` (transaction tracked at height H, evicted at `best_height = H + 1001`):
- Line 208: `1001 >= 1000` → enters `old_unconfirmed_txs` branch (correct)
- Line 215: `confirm_blocks_to_failed_txs[1000][bucket_index]` → index 1000 on a Vec of length 1000 → **Rust panic**

The call chain that sets `count_failure = true`: [4](#0-3) [5](#0-4) [6](#0-5) 

`reject_tx` is only wired for `ConfirmationFraction` and acquires the write lock: [7](#0-6) 

`track_tx` only records transactions at `height == best_height`, confirming `tx_age` is always `best_height - entry_height`: [8](#0-7) 

The existing guard at line 208 is insufficient: it only protects the ring-buffer decrement path; it does not gate the `confirm_blocks_to_failed_txs` write at line 215.

## Impact Explanation

**High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

The panic fires while holding the `RwLock` write guard (`algo.write().reject_tx(tx_hash)`). Depending on the `ckb_util::RwLock` implementation, this either poisons the lock (making all future `commit_block` and `estimate_fee_rate` calls fail with a poisoned-lock error) or propagates the panic to the calling service thread, crashing the node. Both outcomes constitute a crash or permanent denial-of-service of a core subsystem.

## Likelihood Explanation

The `ConfirmationFraction` algorithm must be explicitly configured (`fee_estimator.algorithm = "ConfirmationFraction"`). Once configured, the attack requires no privilege beyond the public `send_transaction` RPC: submit a low-fee transaction at height H, wait 1001 blocks (~2.3 hours at 8 s/block), then submit a conflicting RBF transaction to evict the original. No hashpower, Sybil capability, or special access is required. The attack is repeatable and deterministic. Likelihood is **Medium** given the configuration precondition, but trivially executable once that precondition is met.

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

This mirrors the existing guard on `block_unconfirmed_txs` and silently discards failure samples for transactions older than `MAX_CONFIRM_BLOCKS`, consistent with the `old_unconfirmed_txs` overflow path. Additionally, the `TODO` at line 247 regarding decay of `old_unconfirmed_txs` should be resolved to prevent denominator inflation in `estimate_median`. [9](#0-8) 

## Proof of Concept

1. Configure the node with `fee_estimator.algorithm = "ConfirmationFraction"`.
2. At block height H, call `send_transaction` with transaction T at fee rate just above `min_fee_rate`. The estimator calls `accept_tx` → `track_tx` → `add_unconfirmed_tx`, recording T at height H (requires `height == best_height`).
3. Allow 1001 blocks to be committed. Each block calls `commit_block` → `process_block` → `move_track_window` + `decay`. At block H+1000, `move_track_window` moves T's slot into `old_unconfirmed_txs`. `best_height` is now H+1001.
4. Submit a conflicting RBF transaction to evict T. The tx-pool calls `fee_estimator.reject_tx(&T.hash)`.
5. `reject_tx` → `drop_tx` → `drop_tx_inner(T.hash, true)` → `remove_unconfirmed_tx(H, H+1001, bucket_index, true)`.
6. `tx_age = 1001 >= 1000` → enters `old_unconfirmed_txs` branch (correct), then executes `confirm_blocks_to_failed_txs[1000][bucket_index] += 1f64`.
7. `confirm_blocks_to_failed_txs.len() == 1000`; index 1000 is out of bounds → **Rust panic**.

A unit test can reproduce this directly by constructing a `TxConfirmStat` with `max_confirm_blocks = 1000`, calling `add_unconfirmed_tx(0, fee_rate)`, then `remove_unconfirmed_tx(0, 1001, bucket_index, true)` and observing the panic.

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L22-22)
```rust
const MAX_CONFIRM_BLOCKS: usize = 1000;
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-141)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L208-216)
```rust
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L247-248)
```rust
            // TODO do we need decay the old unconfirmed?
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L428-430)
```rust
    fn drop_tx(&mut self, tx_hash: &Byte32) -> bool {
        self.drop_tx_inner(tx_hash, true).is_some()
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L475-477)
```rust
    pub fn reject_tx(&mut self, tx_hash: &Byte32) {
        let _ = self.drop_tx(tx_hash);
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
