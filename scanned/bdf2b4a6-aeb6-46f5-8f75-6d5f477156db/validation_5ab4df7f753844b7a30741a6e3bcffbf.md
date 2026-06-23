Based on my direct reading of the source code, this is a **real, concrete out-of-bounds panic vulnerability**.

---

### Title
Out-of-Bounds Panic in `remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

### Summary

`TxConfirmStat::remove_unconfirmed_tx` correctly branches on `tx_age >= block_unconfirmed_txs.len()` to handle the `old_unconfirmed_txs` counter, but then unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without the same bound check. When `tx_age > 1000`, this index is out of bounds and Rust panics, crashing the node.

### Finding Description

`confirm_blocks_to_failed_txs` is initialized with length `MAX_CONFIRM_BLOCKS = 1000` (valid indices `0..=999`): [1](#0-0) 

The `remove_unconfirmed_tx` function computes `tx_age` and correctly guards the `block_unconfirmed_txs` / `old_unconfirmed_txs` branch at line 208, but the `count_failure` block at lines 214–216 is **not** guarded: [2](#0-1) 

When `tx_age == 1001` (tx in pool for 1001 blocks), `tx_age - 1 == 1000`, which is past the end of the 1000-element `confirm_blocks_to_failed_txs` vector → **panic**.

The caller `drop_tx_inner` always passes `count_failure = true` when a tx is rejected: [3](#0-2) 

And `reject_tx` (the public production API) calls `drop_tx` which passes `count_failure = true`: [4](#0-3) [5](#0-4) 

### Impact Explanation

An unprivileged attacker submits a valid low-fee-rate transaction that is accepted into the tx-pool and tracked by the fee estimator. After the tx remains unconfirmed for more than 1000 blocks (the default tx-pool expiry is ~3 days ≈ 10,800 blocks at 28 s/block, far exceeding 1000), the tx is eventually evicted by `remove_expired` or `limit_size`. This triggers `reject_tx` → `drop_tx_inner(hash, true)` → `remove_unconfirmed_tx(..., count_failure=true)` with `tx_age > 1000`, causing an out-of-bounds index on `confirm_blocks_to_failed_txs` and a **node process crash (panic)**.

### Likelihood Explanation

- Any valid tx with a fee rate below the typical confirmation threshold can remain unconfirmed for >1000 blocks.
- The tx-pool default expiry is far longer than 1000 blocks, so the condition is naturally reachable without any special setup.
- No privileged access, hashpower, or key material is required — only a valid transaction submission.
- The fee estimator is active on a synced (non-IBD) node, which is the normal operating state.

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` index in `remove_unconfirmed_tx`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This mirrors the existing guard on `block_unconfirmed_txs` at line 208. [6](#0-5) 

### Proof of Concept

1. Submit a valid tx with a very low fee rate at tip height `H`. The fee estimator tracks it via `track_tx` → `add_unconfirmed_tx`.
2. Mine 1001 blocks without including the tx. Each block calls `move_track_window`; after 1000 blocks the tx's slot is moved to `old_unconfirmed_txs`.
3. Trigger tx eviction (e.g., via `remove_expired` or `limit_size`). This calls `reject_tx` on the fee estimator.
4. Inside `remove_unconfirmed_tx`: `tx_age = 1001`, `tx_age - 1 = 1000`, `confirm_blocks_to_failed_txs[1000]` → **index out of bounds: the len is 1000 but the index is 1000** → process panics. [7](#0-6) [1](#0-0)

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L475-477)
```rust
    pub fn reject_tx(&mut self, tx_hash: &Byte32) {
        let _ = self.drop_tx(tx_hash);
    }
```
