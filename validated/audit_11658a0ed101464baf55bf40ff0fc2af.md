### Title
Fee Estimator State Not Rolled Back on Chain Reorganization — (`tx-pool/src/process.rs`)

---

### Summary

During a chain reorganization, `update_tx_pool_for_reorg` calls `fee_estimator.commit_block()` for each newly attached block but performs **no corresponding rollback** for detached blocks. The fee estimator's internal cumulative statistics therefore permanently absorb data from blocks that are no longer on the canonical chain, causing incorrect fee rate estimates after any reorg.

---

### Finding Description

In `tx-pool/src/process.rs`, the `update_tx_pool_for_reorg` function handles reorgs as follows:

```rust
for blk in detached_blocks {
    detached.extend(blk.transactions().into_iter().skip(1))
    // ← no fee_estimator call here
}

for blk in attached_blocks {
    self.fee_estimator.commit_block(&blk);   // ← only attached blocks update the estimator
    attached.extend(blk.transactions().into_iter().skip(1));
}
``` [1](#0-0) 

The `FeeEstimator::commit_block` dispatches to the active algorithm. For `confirmation_fraction::Algorithm`, `commit_block` updates `current_tip` and calls `process_block`, which accumulates data into the following persistent fields:

- `tx_confirm_stat.confirm_blocks_to_confirmed_txs` — per-bucket confirmed-tx counts
- `tx_confirm_stat.confirm_blocks_to_failed_txs` — per-bucket failed-tx counts
- `tx_confirm_stat.block_unconfirmed_txs` — recent-block unconfirmed-tx ring buffer
- `tracked_txs` — map of in-flight tx hashes to their entry height and fee-rate bucket [2](#0-1) 

`commit_block` is the **only** public mutation method that advances the estimator's block-level state: [3](#0-2) 

There is no `detach_block`, `rollback_block`, or equivalent method anywhere in `util/fee-estimator/src/`:



The `FeeEstimator` enum's public API in `mod.rs` exposes only `commit_block`, `accept_tx`, `reject_tx`, `update_ibd_state`, and `estimate_fee_rate` — no rollback path exists: [4](#0-3) 

**Concrete scenario:**

1. Blocks A₁, A₂, A₃ are committed on the main chain. `commit_block` is called for each; their transactions are marked "confirmed" in `tx_confirm_stat` at their respective fee-rate buckets.
2. A reorg occurs: A₁–A₃ are detached; B₁–B₄ are attached. `commit_block` is called for B₁–B₄ only.
3. The estimator now contains confirmation statistics from **both** the detached A-chain and the new B-chain. Transactions from A₁–A₃ that were marked confirmed are never un-marked; their fee-rate bucket counts remain inflated.
4. Subsequent calls to `estimate_fee_rate` (via the `estimate_fee_rate` RPC) compute medians and confirmation rates over this corrupted dataset.

This is structurally identical to the reported H-6 bug: a "last known value" (`lastEthPrice` / the estimator's accumulated block statistics) is advanced for the new chain but never rolled back for the detached chain, causing repeated or phantom accounting of the same events.

---

### Impact Explanation

The `estimate_fee_rate` RPC endpoint returns fee rate recommendations derived entirely from the corrupted estimator state. After a reorg:

- **Over-estimation**: If the detached chain had high-fee blocks, their data inflates the confirmed-tx counts in high-fee buckets, causing the estimator to recommend unnecessarily high fees.
- **Under-estimation**: If the detached chain had low-fee blocks, their data deflates the required fee rate, causing the estimator to recommend fees too low for timely confirmation.

Users and wallets that rely on `estimate_fee_rate` will receive systematically wrong guidance. The fallback path in `tx-pool/src/pool.rs` (`pool_map.estimate_fee_rate`) is unaffected, but it is only used when the primary estimator returns `NoProperFeeRate`. [5](#0-4) 

---

### Likelihood Explanation

Chain reorganizations are a routine, protocol-level event. Any block relayer or miner submitting a valid competing chain of greater total difficulty triggers a reorg — no special privilege is required. Short reorgs (1–3 blocks) occur regularly on mainnet. The fee estimator's state corruption is **permanent** (it accumulates indefinitely) and is only reset when the node enters IBD mode (`update_ibd_state(true)`), which clears all state via `clear()`. [6](#0-5) 

The entry path is: unprivileged block relayer → `ChainService::process_block` → `update_tx_pool_for_reorg` notification → `TxPoolService` reorg handler → `update_tx_pool_for_reorg` in `process.rs`. [7](#0-6) [8](#0-7) 

---

### Recommendation

Implement a `detach_block` (or `rollback_block`) method on both `confirmation_fraction::Algorithm` and `weight_units_flow::Algorithm` that reverses the effects of `commit_block`. In `update_tx_pool_for_reorg`, call it for each detached block before calling `commit_block` for each attached block:

```rust
for blk in &detached_blocks {
    self.fee_estimator.detach_block(blk);   // ← add this
}
for blk in &attached_blocks {
    self.fee_estimator.commit_block(blk);
}
```

For `confirmation_fraction`, this requires storing per-block snapshots of the delta applied to `tx_confirm_stat` so they can be subtracted on rollback, or alternatively clearing and replaying from a known-good checkpoint height.

---

### Proof of Concept

1. Start a CKB node with `ConfirmationFraction` fee estimator enabled.
2. Mine 50 blocks with high-fee transactions. Call `estimate_fee_rate` — note the returned value.
3. Trigger a reorg of depth 10 by submitting a competing chain of 11 blocks with low-fee transactions.
4. Call `estimate_fee_rate` again. The returned value will remain elevated (reflecting the detached high-fee blocks) rather than dropping to reflect the new low-fee chain, demonstrating that the detached blocks' statistics were never removed from the estimator.

### Citations

**File:** tx-pool/src/process.rs (L818-825)
```rust
        for blk in detached_blocks {
            detached.extend(blk.transactions().into_iter().skip(1))
        }

        for blk in attached_blocks {
            self.fee_estimator.commit_block(&blk);
            attached.extend(blk.transactions().into_iter().skip(1));
        }
```

**File:** tx-pool/src/process.rs (L945-970)
```rust
    pub(crate) async fn estimate_fee_rate(
        &self,
        estimate_mode: EstimateMode,
        enable_fallback: bool,
    ) -> Result<FeeRate, AnyError> {
        let all_entry_info = self.tx_pool.read().await.get_all_entry_info();
        match self
            .fee_estimator
            .estimate_fee_rate(estimate_mode, all_entry_info)
        {
            Ok(fee_rate) => Ok(fee_rate),
            Err(err) => {
                if enable_fallback {
                    let target_blocks =
                        FeeEstimator::target_blocks_for_estimate_mode(estimate_mode);
                    self.tx_pool
                        .read()
                        .await
                        .estimate_fee_rate(target_blocks)
                        .map_err(Into::into)
                } else {
                    Err(err.into())
                }
            }
        }
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L44-87)
```rust
struct TxConfirmStat {
    min_fee_rate: FeeRate,
    /// per bucket stat
    bucket_stats: Vec<BucketStat>,
    /// bucket upper bound fee_rate => bucket index
    fee_rate_to_bucket: BTreeMap<FeeRate, usize>,
    /// confirm_blocks => bucket index => confirmed txs count
    confirm_blocks_to_confirmed_txs: Vec<Vec<f64>>,
    /// confirm_blocks => bucket index => failed txs count
    confirm_blocks_to_failed_txs: Vec<Vec<f64>>,
    /// Track recent N blocks unconfirmed txs
    /// tracked block index => bucket index => TxTracker
    block_unconfirmed_txs: Vec<Vec<usize>>,
    decay_factor: f64,
}

#[derive(Clone)]
struct TxRecord {
    height: u64,
    bucket_index: usize,
    fee_rate: FeeRate,
}

/// Estimator track new block and tx_pool to collect data
/// we track every new tx enter txpool and record the tip height and fee_rate,
/// when tx is packed into a new block or dropped by txpool,
/// we get a sample about how long a tx with X fee_rate can get confirmed or get dropped.
///
/// In inner, we group samples by predefined fee_rate buckets.
/// To estimator fee_rate for a confirm target(how many blocks that a tx can get committed),
/// we travel through fee_rate buckets, try to find a fee_rate X to let a tx get committed
/// with high probabilities within confirm target blocks.
///
#[derive(Clone)]
pub struct Algorithm {
    best_height: u64,
    start_height: u64,
    /// a data struct to track tx confirm status
    tx_confirm_stat: TxConfirmStat,
    tracked_txs: HashMap<Byte32, TxRecord>,

    current_tip: BlockNumber,
    is_ready: bool,
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L463-467)
```rust
    pub fn commit_block(&mut self, block: &BlockView) {
        let tip_number = block.number();
        self.current_tip = tip_number;
        self.process_block(tip_number, block.tx_hashes().iter().map(ToOwned::to_owned));
    }
```

**File:** util/fee-estimator/src/estimator/mod.rs (L28-105)
```rust
impl FeeEstimator {
    /// Creates a new dummy fee estimator.
    pub fn new_dummy() -> Self {
        FeeEstimator::Dummy
    }

    /// Creates a new confirmation fraction fee estimator.
    pub fn new_confirmation_fraction() -> Self {
        let algo = confirmation_fraction::Algorithm::new();
        FeeEstimator::ConfirmationFraction(Arc::new(RwLock::new(algo)))
    }

    /// Target blocks for the provided estimate mode.
    pub const fn target_blocks_for_estimate_mode(estimate_mode: EstimateMode) -> BlockNumber {
        match estimate_mode {
            EstimateMode::NoPriority => constants::DEFAULT_TARGET,
            EstimateMode::LowPriority => constants::LOW_TARGET,
            EstimateMode::MediumPriority => constants::MEDIUM_TARGET,
            EstimateMode::HighPriority => constants::HIGH_TARGET,
        }
    }

    /// Creates a new weight-units flow fee estimator.
    pub fn new_weight_units_flow() -> Self {
        let algo = weight_units_flow::Algorithm::new();
        FeeEstimator::WeightUnitsFlow(Arc::new(RwLock::new(algo)))
    }

    /// Updates the IBD state.
    pub fn update_ibd_state(&self, in_ibd: bool) {
        match self {
            Self::Dummy => {}
            Self::ConfirmationFraction(algo) => algo.write().update_ibd_state(in_ibd),
            Self::WeightUnitsFlow(algo) => algo.write().update_ibd_state(in_ibd),
        }
    }

    /// Commits a block.
    pub fn commit_block(&self, block: &BlockView) {
        match self {
            Self::Dummy => {}
            Self::ConfirmationFraction(algo) => algo.write().commit_block(block),
            Self::WeightUnitsFlow(algo) => algo.write().commit_block(block),
        }
    }

    /// Accepts a tx.
    pub fn accept_tx(&self, tx_hash: Byte32, info: TxEntryInfo) {
        match self {
            Self::Dummy => {}
            Self::ConfirmationFraction(algo) => algo.write().accept_tx(tx_hash, info),
            Self::WeightUnitsFlow(algo) => algo.write().accept_tx(info),
        }
    }

    /// Rejects a tx.
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
    }

    /// Estimates fee rate.
    pub fn estimate_fee_rate(
        &self,
        estimate_mode: EstimateMode,
        all_entry_info: TxPoolEntryInfo,
    ) -> Result<FeeRate, Error> {
        let target_blocks = Self::target_blocks_for_estimate_mode(estimate_mode);
        match self {
            Self::Dummy => Err(Error::Dummy),
            Self::ConfirmationFraction(algo) => algo.read().estimate_fee_rate(target_blocks),
            Self::WeightUnitsFlow(algo) => {
                algo.read().estimate_fee_rate(target_blocks, all_entry_info)
            }
        }
    }
```

**File:** chain/src/verify.rs (L385-398)
```rust
            let tx_pool_controller = self.shared.tx_pool_controller();
            if tx_pool_controller.service_started() {
                if let Err(e) = tx_pool_controller.update_tx_pool_for_reorg(
                    fork.detached_blocks().clone(),
                    fork.attached_blocks().clone(),
                    fork.detached_proposal_id().clone(),
                    new_snapshot,
                ) {
                    error!("[verify block] notify update_tx_pool_for_reorg error {}", e);
                }
                if let Err(e) = tx_pool_controller.update_ibd_state(in_ibd) {
                    error!("Notify update_ibd_state error {}", e);
                }
            }
```

**File:** tx-pool/src/service.rs (L697-731)
```rust
        let signal_receiver = self.signal_receiver;
        self.handle.spawn(async move {
            loop {
                tokio::select! {
                    Some(message) = reorg_receiver.recv() => {
                        let Notify {
                            arguments: (detached_blocks, attached_blocks, detached_proposal_id, snapshot),
                        } = message;
                        let snapshot_clone = Arc::clone(&snapshot);
                        let detached_blocks_clone = detached_blocks.clone();
                        service.update_block_assembler_before_tx_pool_reorg(
                            detached_blocks_clone,
                            snapshot_clone
                        ).await;

                        let snapshot_clone = Arc::clone(&snapshot);
                        service
                        .update_tx_pool_for_reorg(
                            detached_blocks,
                            attached_blocks,
                            detached_proposal_id,
                            snapshot_clone,
                        )
                        .await;

                        service.update_block_assembler_after_tx_pool_reorg().await;
                    },
                    _ = signal_receiver.cancelled() => {
                        info!("TxPool reorg process service received exit signal, exit now");
                        break
                    },
                    else => break,
                }
            }
        });
```
