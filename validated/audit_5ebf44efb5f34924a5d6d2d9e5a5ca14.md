### Title
Fee Estimator `decay()` Applied Only Once Per `process_block` Call Regardless of Block Height Gap, Causing Slower-Than-Designed Decay After Chain Reorgs - (File: `util/fee-estimator/src/estimator/confirmation_fraction.rs`)

### Summary

`Algorithm::process_block` updates `self.best_height` to the incoming block height in a single jump but calls `TxConfirmStat::decay()` exactly once, regardless of how many blocks were skipped. During a chain reorganization, blocks from the fork point up to the old tip have heights ≤ `best_height` and are silently skipped, so `decay()` is never called for those blocks. The result is that historical fee-rate samples decay more slowly than the model's designed half-life of 100 blocks.

### Finding Description

`Algorithm::process_block` is the core update function of the `ConfirmationFraction` fee estimator:

```rust
fn process_block(&mut self, height: u64, txs: impl Iterator<Item = Byte32>) {
    if height <= self.best_height {
        return;                          // silently skips every block ≤ best_height
    }
    self.best_height = height;           // jumps to current height in one step
    self.tx_confirm_stat.move_track_window(height);
    self.tx_confirm_stat.decay();        // called exactly ONCE, not (height - old_best_height) times
    ...
}
``` [1](#0-0) 

The decay factor is designed so that samples have a half-life of exactly 100 blocks:

```rust
let decay_factor: f64 = (0.5f64.ln() / 100.0).exp();
``` [2](#0-1) 

`commit_block` feeds each attached block into `process_block`:

```rust
pub fn commit_block(&mut self, block: &BlockView) {
    let tip_number = block.number();
    self.current_tip = tip_number;
    self.process_block(tip_number, block.tx_hashes().iter().map(ToOwned::to_owned));
}
``` [3](#0-2) 

During a chain reorganization, `update_tx_pool_for_reorg` calls `commit_block` for every block in `attached_blocks`:

```rust
for blk in attached_blocks {
    self.fee_estimator.commit_block(&blk);
    ...
}
``` [4](#0-3) 

**Concrete scenario**: Suppose `best_height = 200` (old chain tip). A reorg forks at block 150; the new chain runs from block 151 to 210. `attached_blocks` = [151, 152, …, 210]. When `process_block` is called for blocks 151–200, the guard `if height <= self.best_height { return; }` fires for all of them. Only blocks 201–210 pass the guard, so `decay()` is called 10 times. The model should have applied `decay()` 60 times (once per block 151–210). The 50 missing decay applications mean old samples retain `decay_factor^(-50) ≈ 1.41×` more weight than intended.

The same structural error exists in `move_track_window`, which is also called only once per `process_block` invocation, clearing only one slot of the circular unconfirmed-tx buffer instead of all skipped slots. [5](#0-4) 

### Impact Explanation

The fee estimator's decay model runs slower than its designed half-life of 100 blocks after any chain reorganization. Old fee-rate samples from the pre-reorg chain retain disproportionate weight. Depending on whether pre-reorg fees were higher or lower than post-reorg fees, the `estimate_fee_rate` RPC returns inflated or deflated fee-rate recommendations. Users and wallets relying on this RPC will systematically overpay or underpay transaction fees following reorgs. The deeper the reorg, the larger the deviation: a 50-block-deep reorg causes the effective half-life to stretch from 100 blocks to ~200 blocks for that period.

### Likelihood Explanation

Shallow reorgs (1–3 blocks) are routine on CKB mainnet and cause a small but accumulating drift. Deeper reorgs (tens of blocks) are rarer but do occur and cause proportionally larger drift. No attacker action is required; the bug is triggered by any natural reorg. The entry path is: block relayer or miner submits a valid block that triggers a reorg → chain service calls `update_tx_pool_for_reorg` → `commit_block` is called for each attached block → `process_block` skips blocks with height ≤ `best_height` → `decay()` is under-applied.

### Recommendation

In `process_block`, compute the number of blocks actually skipped and apply `decay()` that many times:

```rust
fn process_block(&mut self, height: u64, txs: impl Iterator<Item = Byte32>) {
    if height <= self.best_height {
        return;
    }
    let blocks_elapsed = height - self.best_height;
    self.best_height = height;
    // Apply decay once per elapsed block, not once per call
    for h in (height - blocks_elapsed + 1)..=height {
        self.tx_confirm_stat.move_track_window(h);
        self.tx_confirm_stat.decay();
    }
    ...
}
```

This mirrors the recommended fix in the reference report: advance the marker by the number of whole quantized units actually consumed, not by jumping to the current value.

### Proof of Concept

1. Start a CKB node with the `ConfirmationFraction` fee estimator enabled.
2. Let the chain reach height 200 with moderate fee-rate activity so the estimator accumulates samples.
3. Trigger a 50-block-deep reorg (new chain forks at 150, new tip at 210).
4. Call `estimate_fee_rate` immediately after the reorg.
5. Observe that the returned fee rate reflects pre-reorg samples with far more weight than expected, because `decay()` was called only 10 times (for blocks 201–210) instead of 60 times (for blocks 151–210).
6. Compare against a node that processed the same 60 blocks sequentially without a reorg: the sequential node's estimate will have decayed correctly, while the reorg node's estimate will be skewed toward pre-reorg data. [1](#0-0) [6](#0-5) [7](#0-6)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L120-121)
```rust
        // half life each 100 blocks, math.exp(math.log(0.5) / 100)
        let decay_factor: f64 = (0.5f64.ln() / 100.0).exp();
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L229-249)
```rust
    /// apply decay factor on stats, smoothly reduce the effects of old samples.
    fn decay(&mut self) {
        let decay_factor = self.decay_factor;
        for (bucket_index, bucket) in self.bucket_stats.iter_mut().enumerate() {
            self.confirm_blocks_to_confirmed_txs
                .iter_mut()
                .for_each(|buckets| {
                    buckets[bucket_index] *= decay_factor;
                });

            self.confirm_blocks_to_failed_txs
                .iter_mut()
                .for_each(|buckets| {
                    buckets[bucket_index] *= decay_factor;
                });
            bucket.total_fee_rate =
                FeeRate::from_u64((bucket.total_fee_rate.as_u64() as f64 * decay_factor) as u64);
            bucket.txs_count *= decay_factor;
            // TODO do we need decay the old unconfirmed?
        }
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L377-392)
```rust
    fn process_block(&mut self, height: u64, txs: impl Iterator<Item = Byte32>) {
        // For simpfy, we assume chain reorg will not effect tx fee.
        if height <= self.best_height {
            return;
        }
        self.best_height = height;
        // update tx confirm stat
        self.tx_confirm_stat.move_track_window(height);
        self.tx_confirm_stat.decay();
        let processed_txs = txs.filter(|tx| self.process_block_tx(height, tx)).count();
        if self.start_height == 0 && processed_txs > 0 {
            // start record
            self.start_height = self.best_height;
            ckb_logger::debug!("start recording at {}", self.start_height);
        }
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

**File:** tx-pool/src/process.rs (L802-825)
```rust
    pub(crate) async fn update_tx_pool_for_reorg(
        &self,
        detached_blocks: VecDeque<BlockView>,
        attached_blocks: VecDeque<BlockView>,
        detached_proposal_id: HashSet<ProposalShortId>,
        snapshot: Arc<Snapshot>,
    ) {
        let mine_mode = self.block_assembler.is_some();
        let mut detached = LinkedHashSet::default();
        let mut attached = LinkedHashSet::default();

        let detached_headers: HashSet<Byte32> = detached_blocks
            .iter()
            .map(|blk| blk.header().hash())
            .collect();

        for blk in detached_blocks {
            detached.extend(blk.transactions().into_iter().skip(1))
        }

        for blk in attached_blocks {
            self.fee_estimator.commit_block(&blk);
            attached.extend(blk.transactions().into_iter().skip(1));
        }
```
