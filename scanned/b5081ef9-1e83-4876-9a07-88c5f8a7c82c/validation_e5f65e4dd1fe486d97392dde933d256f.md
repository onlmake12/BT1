The code is fully readable. Let me trace the exact execution path.

**Step 1 — `accept_tx` with `current_tip == 0`**

`accept_tx` passes `self.current_tip` directly to `track_tx`: [1](#0-0) 

**Step 2 — `track_tx` guard does NOT block height=0**

The only guard is `height != self.best_height`. When both are 0 (freshly initialized or just cleared), `0 != 0` is `false`, so the tx IS inserted into `tracked_txs` at height 0: [2](#0-1) 

**Step 3 — `remove_unconfirmed_tx` out-of-bounds**

`confirm_blocks_to_failed_txs` has exactly `MAX_CONFIRM_BLOCKS = 1000` entries (indices 0–999). When `tx_age >= 1000`, the code takes the `old_unconfirmed_txs` branch — but then unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]`. If `tx_age = 1001`, that is index 1000, which is out of bounds → **Rust panic**: [3](#0-2) 

Both `confirm_blocks_to_failed_txs` and `block_unconfirmed_txs` are allocated with the same `max_confirm_blocks = 1000` length: [4](#0-3) 

**Step 4 — The missing guard (contrast with `WeightUnitsFlow`)**

`WeightUnitsFlow::accept_tx` has an explicit `current_tip == 0` early-return guard. `ConfirmationFraction::accept_tx` has no such guard: [5](#0-4) 

**Step 5 — IBD exit resets state to zero**

`update_ibd_state(false)` calls `clear()`, which resets `current_tip = 0` and `best_height = 0`, then sets `is_ready = true`. Any tx accepted before the first `commit_block` after IBD exit is tracked at height 0: [6](#0-5) 

**Step 6 — Configuration requirement**

`ConfirmationFraction` is not the default; it must be explicitly configured. The default is `Dummy` (no-op): [7](#0-6) 

---

**Full exploit chain:**
1. Operator configures `algorithm = "ConfirmationFraction"`
2. Node exits IBD → `clear()` → `current_tip = 0`, `best_height = 0`, `is_ready = true`
3. Attacker submits a tx before the first `commit_block` → tracked at height 0
4. Node processes 1001 blocks → `best_height = 1001`
5. Tx is evicted (pool full, reorg, expiry) → `reject_tx` → `drop_tx_inner(hash, true)` → `remove_unconfirmed_tx(0, 1001, bucket_index, true)` → `tx_age = 1001` → `confirm_blocks_to_failed_txs[1000]` → **index out of bounds panic → node crash**

---

### Title
Out-of-bounds panic in `ConfirmationFraction::remove_unconfirmed_tx` when tx tracked at height 0 ages beyond `MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

### Summary
`ConfirmationFraction::accept_tx` lacks the `current_tip == 0` guard present in `WeightUnitsFlow::accept_tx`. A tx submitted before the first `commit_block` after IBD exit is tracked at height 0. When that tx is later rejected after 1001+ blocks, `remove_unconfirmed_tx` accesses `confirm_blocks_to_failed_txs[tx_age - 1]` with `tx_age = 1001`, which is index 1000 on a vector of length 1000, causing a Rust index-out-of-bounds panic and crashing the node.

### Finding Description
`TxConfirmStat` allocates `confirm_blocks_to_failed_txs` with exactly `MAX_CONFIRM_BLOCKS = 1000` entries. In `remove_unconfirmed_tx`, when `tx_age >= block_unconfirmed_txs.len()` (i.e., ≥ 1000), the code correctly routes to the `old_unconfirmed_txs` counter, but then unconditionally executes `self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64` without bounding `tx_age` to `MAX_CONFIRM_BLOCKS`. For `tx_age = 1001`, this accesses index 1000 on a length-1000 vector.

The precondition — a tx tracked at height 0 — is reachable because `track_tx` only checks `height != self.best_height`, and both fields are 0 after `clear()` is called on IBD exit. `WeightUnitsFlow` avoids this with an explicit `if self.current_tip == 0 { return; }` guard that `ConfirmationFraction` is missing.

### Impact Explanation
Node process crash (panic) on the first tx rejection after 1001+ blocks have been processed post-IBD-exit, when `ConfirmationFraction` is configured. Impact is denial of service: the node goes down and must be restarted.

### Likelihood Explanation
Requires three conditions: (1) non-default `ConfirmationFraction` configuration, (2) a tx submitted in the window between IBD exit and the first `commit_block`, and (3) that tx surviving in the pool for 1001+ blocks before being rejected. Condition (1) limits the affected population. Conditions (2) and (3) are timing-dependent but achievable: the IBD-exit window can be seconds to minutes, and a low-fee-rate tx can linger in the pool for hours. An attacker who monitors IBD exit (observable via P2P sync state) can deliberately submit a low-fee tx at the right moment and wait.

### Recommendation
Add the same guard present in `WeightUnitsFlow::accept_tx` to `ConfirmationFraction::accept_tx`:
```rust
pub fn accept_tx(&mut self, tx_hash: Byte32, info: TxEntryInfo) {
    if self.current_tip == 0 {
        return;
    }
    ...
}
```
Additionally, bound `tx_age` in `remove_unconfirmed_tx` before indexing `confirm_blocks_to_failed_txs`:
```rust
if count_failure {
    let capped_age = tx_age.min(self.confirm_blocks_to_failed_txs.len());
    self.confirm_blocks_to_failed_txs[capped_age - 1][bucket_index] += 1f64;
}
```

### Proof of Concept
```rust
#[test]
fn test_height_zero_oob_panic() {
    let mut algo = Algorithm::new();
    // Simulate IBD exit: is_ready = true, current_tip = 0, best_height = 0
    algo.update_ibd_state(false);

    // Submit tx before any commit_block (current_tip == best_height == 0)
    let tx_hash = ckb_types::packed::Byte32::default();
    algo.accept_tx(tx_hash.clone(), TxEntryInfo { fee: 1000, size: 100, cycles: 100 });

    // Advance 1001 blocks
    for i in 1u64..=1001 {
        let block = /* build BlockView at height i */;
        algo.commit_block(&block);
    }

    // Reject the tx — triggers remove_unconfirmed_tx(0, 1001, _, true)
    // confirm_blocks_to_failed_txs[1000] → index out of bounds → panic
    algo.reject_tx(&tx_hash); // should not panic, but does
}
```

### Citations

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L395-414)
```rust
    fn track_tx(&mut self, tx_hash: Byte32, fee_rate: FeeRate, height: u64) {
        if self.tracked_txs.contains_key(&tx_hash) {
            // already in track
            return;
        }
        if height != self.best_height {
            // ignore wrong height txs
            return;
        }
        if let Some(bucket_index) = self.tx_confirm_stat.add_unconfirmed_tx(height, fee_rate) {
            self.tracked_txs.insert(
                tx_hash,
                TxRecord {
                    height,
                    bucket_index,
                    fee_rate,
                },
            );
        }
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L443-461)
```rust
    pub fn update_ibd_state(&mut self, in_ibd: bool) {
        if self.is_ready {
            if in_ibd {
                self.clear();
                self.is_ready = false;
            }
        } else if !in_ibd {
            self.clear();
            self.is_ready = true;
        }
    }

    fn clear(&mut self) {
        self.best_height = 0;
        self.start_height = 0;
        self.tx_confirm_stat = Default::default();
        self.tracked_txs.clear();
        self.current_tip = 0;
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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L153-156)
```rust
    pub fn accept_tx(&mut self, info: TxEntryInfo) {
        if self.current_tip == 0 {
            return;
        }
```

**File:** shared/src/shared_builder.rs (L406-414)
```rust
        let fee_estimator_algo = fee_estimator_config
            .map(|config| config.algorithm)
            .unwrap_or(None);
        let fee_estimator = match fee_estimator_algo {
            Some(FeeEstimatorAlgo::WeightUnitsFlow) => FeeEstimator::new_weight_units_flow(),
            Some(FeeEstimatorAlgo::ConfirmationFraction) => {
                FeeEstimator::new_confirmation_fraction()
            }
            None => FeeEstimator::new_dummy(),
```
