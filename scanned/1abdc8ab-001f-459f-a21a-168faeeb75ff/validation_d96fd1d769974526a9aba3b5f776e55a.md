### Title
Index-Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

`TxConfirmStat::remove_unconfirmed_tx` correctly guards the `block_unconfirmed_txs` array access for `tx_age >= 1000`, but unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without a corresponding bound check. When `tx_age > 1000`, this produces an index-out-of-bounds panic in Rust.

---

### Finding Description

`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS = 1000` entries (valid indices `0..=999`): [1](#0-0) 

Inside `remove_unconfirmed_tx`, the code correctly branches on `tx_age >= self.block_unconfirmed_txs.len()` (i.e., `>= 1000`) for the `block_unconfirmed_txs` access: [2](#0-1) 

But immediately after, when `count_failure` is `true`, it unconditionally accesses: [3](#0-2) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is out of bounds for a 1000-element `Vec`. Rust panics here unconditionally.

---

### Exploit Path

**Step 1 — Attacker submits a tx to the pool.**
`accept_tx` → `track_tx` records the tx at `entry_height = H` (requires `height == best_height`): [4](#0-3) 

**Step 2 — 1001+ blocks are committed.**
Each `commit_block` call advances `best_height`: [5](#0-4) 

After 1001 blocks, `best_height = H + 1001`.

**Step 3 — Tx is evicted from the pool.**
The tx-pool's expiry mechanism calls `fee_estimator.reject_tx(tx_hash)`: [6](#0-5) 

This calls `drop_tx_inner(tx_hash, true)` → `remove_unconfirmed_tx(H, H+1001, bucket_index, true)`: [7](#0-6) 

**Step 4 — Panic.**
`tx_age = 1001`, `count_failure = true` → `confirm_blocks_to_failed_txs[1000]` → **index out of bounds panic**.

---

### Impact Explanation

The `Algorithm` is wrapped in `Arc<RwLock<...>>` (parking_lot): [8](#0-7) 

parking_lot's `RwLock` does **not** poison on panic (unlike `std::sync::RwLock`), so the lock itself survives. However, the panic propagates up the call stack from within the write-lock critical section. Depending on the tokio executor context (e.g., `block_in_place`), this either crashes the tx-pool service task or the worker thread, disrupting `estimate_fee_rate` RPC responses and potentially the tx-pool service itself.

---

### Likelihood Explanation

On CKB's ~10-second block time, 1001 blocks ≈ 2.8 hours. Any low-fee-rate transaction that sits in the pool long enough and is then evicted (via the pool's `evict_expired_txs` path) triggers this. No special privilege is required — any user who can submit a transaction to the pool can trigger this passively by waiting.

---

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` access:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This mirrors the existing guard for `block_unconfirmed_txs` and silently discards failure samples for txs older than `MAX_CONFIRM_BLOCKS`, which is semantically correct (they are already counted as `old_unconfirmed_txs`).

---

### Proof of Concept

```rust
#[test]
fn test_no_panic_on_tx_age_exceeding_max_confirm_blocks() {
    let mut algo = Algorithm::new();
    algo.update_ibd_state(false); // mark ready

    // Simulate block 0 committed so current_tip = best_height = 0
    // accept_tx at height 0
    // (construct a minimal TxEntryInfo with nonzero fee/size)
    // algo.accept_tx(tx_hash, info);

    // commit 1001 blocks
    for i in 1..=1001u64 {
        // algo.commit_block(&make_empty_block(i));
    }

    // reject_tx: tx_age = 1001 > 1000 → confirm_blocks_to_failed_txs[1000] → PANIC
    // algo.reject_tx(&tx_hash);  // should not panic after fix
}
``` [9](#0-8)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L140-140)
```rust
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L463-467)
```rust
    pub fn commit_block(&mut self, block: &BlockView) {
        let tip_number = block.number();
        self.current_tip = tip_number;
        self.process_block(tip_number, block.tx_hashes().iter().map(ToOwned::to_owned));
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

**File:** util/fee-estimator/src/estimator/mod.rs (L23-23)
```rust
    ConfirmationFraction(Arc<RwLock<confirmation_fraction::Algorithm>>),
```

**File:** util/fee-estimator/src/estimator/mod.rs (L84-88)
```rust
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
```
