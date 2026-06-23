Looking at the exact code in `remove_unconfirmed_tx`:

The vulnerability is real. Here is the precise analysis:

---

### Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` via `confirm_blocks_to_failed_txs` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

### Summary
`remove_unconfirmed_tx` guards the `block_unconfirmed_txs` access against `tx_age >= 1000`, but applies **no equivalent guard** before indexing `confirm_blocks_to_failed_txs[tx_age - 1]`. When a tracked tx is rejected after 1001+ blocks, `tx_age - 1 >= 1000` is an out-of-bounds index into a `Vec` of length 1000, causing a Rust index-out-of-bounds panic.

### Finding Description

`confirm_blocks_to_failed_txs` is allocated with length `MAX_CONFIRM_BLOCKS = 1000` (valid indices 0–999): [1](#0-0) 

In `remove_unconfirmed_tx`, `tx_age` is computed as `tip_height - entry_height`: [2](#0-1) 

The existing guard at line 208 only protects the `block_unconfirmed_txs` branch: [3](#0-2) 

But the `count_failure` branch at line 214–216 has **no bounds check**: [4](#0-3) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is past the end of the 1000-element `Vec`, causing a panic.

### Impact Explanation

A Rust index-out-of-bounds panic is not recoverable inline. The fee estimator is called from the tx-pool service. A panic there unwinds the tx-pool thread, crashing the CKB node process (or rendering the tx-pool permanently non-functional, depending on the thread supervision model). This is a remote DoS: the node stops processing transactions and blocks.

### Likelihood Explanation

The attacker path requires no privilege:

1. Submit any valid transaction via RPC (`send_transaction`) or P2P relay — this calls `accept_tx` → `track_tx`, recording `entry_height = best_height`. [5](#0-4) 

2. The tx stays in the pool unconfirmed for 1001+ blocks. `move_track_window` moves the slot counter to `old_unconfirmed_txs` but **never removes the tx from `tracked_txs`**, so the record persists indefinitely. [6](#0-5) 

3. Any pool eviction event (conflict, capacity pressure, expiry) triggers `reject_tx` → `drop_tx` (with `count_failure=true`) → `drop_tx_inner` → `remove_unconfirmed_tx`. [7](#0-6) 

At ~10-second block times, 1001 blocks is ~2.8 hours — well within the lifetime of a low-fee tx that never gets mined.

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` access, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

Or use `get_mut` to silently skip out-of-range ages (ages beyond `MAX_CONFIRM_BLOCKS` carry no useful statistical signal anyway):

```rust
if count_failure {
    if let Some(row) = self.confirm_blocks_to_failed_txs.get_mut(tx_age - 1) {
        row[bucket_index] += 1f64;
    }
}
```

### Proof of Concept

```rust
#[test]
fn test_oob_panic_on_reject_after_1001_blocks() {
    let mut algo = Algorithm::new();
    algo.update_ibd_state(false); // mark ready

    // Simulate best_height = 0 via a genesis-like block commit
    // (commit_block sets best_height; track_tx requires height == best_height)
    // Track a tx at height 0
    let tx_hash = ckb_types::packed::Byte32::zero();
    let info = TxEntryInfo { fee: 1000.into(), size: 200, cycles: 1000, .. Default::default() };
    algo.accept_tx(tx_hash.clone(), info);

    // Advance 1001 blocks without including the tx
    for i in 1u64..=1001 {
        let block = /* build empty BlockView at height i */;
        algo.commit_block(&block);
    }

    // This must NOT panic; before the fix it panics with index out of bounds
    algo.reject_tx(&tx_hash);
}
```

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L140-140)
```rust
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L204-204)
```rust
        let tx_age = tip_height.saturating_sub(entry_height) as usize;
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L219-227)
```rust
    fn move_track_window(&mut self, height: u64) {
        let block_index = (height % (self.block_unconfirmed_txs.len() as u64)) as usize;
        for bucket_index in 0..self.bucket_stats.len() {
            // mark unconfirmed txs as old_unconfirmed_txs
            self.bucket_stats[bucket_index].old_unconfirmed_txs +=
                self.block_unconfirmed_txs[block_index][bucket_index];
            self.block_unconfirmed_txs[block_index][bucket_index] = 0;
        }
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L427-430)
```rust
    /// tx removed from txpool
    fn drop_tx(&mut self, tx_hash: &Byte32) -> bool {
        self.drop_tx_inner(tx_hash, true).is_some()
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
