### Title
Transient Transaction Flood Permanently Inflates `WeightUnitsFlow` Fee Rate Estimate Until Historical Window Expires - (File: `util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

The `WeightUnitsFlow` fee estimator records every transaction that enters the tx-pool as a permanent "flow" data point, but **never removes that data when transactions are evicted or rejected**. An unprivileged attacker can temporarily flood the tx-pool with high-fee transactions, causing the estimator to record an artificially high flow rate. After evicting those transactions (e.g., via double-spend), the inflated flow data persists for up to `2 × target_blocks` blocks (up to ~256 blocks, roughly 2 hours for the default target). During this window, every call to `estimate_fee_rate` returns an artificially elevated fee rate, causing users and wallets to systematically overpay transaction fees.

---

### Finding Description

The `WeightUnitsFlow` algorithm in `util/fee-estimator/src/estimator/weight_units_flow.rs` maintains a `txs: HashMap<BlockNumber, Vec<TxStatus>>` map that records the weight and fee rate of every transaction accepted into the tx-pool, keyed by the block number at which it was accepted.

**Step 1 — Flow data is recorded on every `accept_tx`:** [1](#0-0) 

Every transaction entering the tx-pool is appended to `self.txs[current_tip]`. This data is later used to compute the "flow speed" — the rate at which new weight is expected to enter the mempool.

**Step 2 — `reject_tx` is a no-op for `WeightUnitsFlow`:** [2](#0-1) 

When a transaction is evicted, dropped, or rejected from the tx-pool, `reject_tx` is called on the `FeeEstimator`. For `WeightUnitsFlow`, this is explicitly a no-op — the flow data recorded in `self.txs` is **never decremented or corrected**.

**Step 3 — Inflated flow data drives the fee rate estimate:** [3](#0-2) 

`do_estimate` computes `flow_speed_buckets` by summing all `self.txs` entries within the historical window and dividing by `historical_blocks`. Artificially injected entries inflate this speed.

**Step 4 — Inflated flow speed causes higher fee rate recommendations:** [4](#0-3) 

The condition `current_weight + added_weight <= removed_weight` determines which fee bucket "passes." An inflated `added_weight` (from inflated `flow_speed`) causes more buckets to fail, pushing the recommended fee rate upward.

**Step 5 — Stale data persists for up to `2 × target_blocks` blocks:** [5](#0-4) 

Data is only expired when a new block is committed. For the default `MAX_TARGET = 128` blocks, `historical_blocks = 256` blocks (~2 hours at ~28s/block). The inflated flow data remains active for this entire window.

**The `historical_blocks` constant:** [6](#0-5) 

---

### Impact Explanation

Any RPC caller or wallet that queries `estimate_fee_rate` during the poisoned window receives an artificially elevated fee rate. Users who trust this estimate will overpay transaction fees. The effect persists for up to ~2 hours (256 blocks at the default target), affecting all fee-sensitive users of the node during that window. This is an economic harm analogous to the RAACMinter emission rate inflation: a transient state manipulation causes a persistent, incorrect rate to be served to downstream consumers.

---

### Likelihood Explanation

The attack requires only the ability to submit transactions to the tx-pool (standard `send_transaction` RPC, available to any unprivileged user) and then evict them (e.g., by submitting a conflicting double-spend transaction). No privileged access, key material, or majority hashpower is required. The cost is the transaction fees paid on any transactions that accidentally get confirmed, but the attacker can minimize this by using transactions that are structurally valid but reference already-spent inputs, ensuring they are rejected quickly. The attack is repeatable and can be sustained across multiple historical windows.

---

### Recommendation

1. **Track evictions in `WeightUnitsFlow`:** Implement a proper `reject_tx` handler that removes or subtracts the weight of evicted transactions from `self.txs`. This mirrors how `ConfirmationFraction` handles `drop_tx`.

2. **Use a time-weighted or block-weighted average:** Rather than a raw sum over the historical window, weight recent blocks more heavily so that a single-block spike has diminishing influence over time.

3. **Cap per-block flow contribution:** Limit the maximum weight contribution from any single block in `self.txs` to a multiple of `MAX_BLOCK_BYTES`, preventing a single flood block from dominating the flow estimate.

4. **Cross-validate flow against confirmed block data:** Anchor the flow estimate to the actual weight of transactions confirmed in recent blocks, which cannot be manipulated by tx-pool flooding alone.

---

### Proof of Concept

**Setup:** Node is running with `WeightUnitsFlow` fee estimator. Normal mempool has moderate activity; `estimate_fee_rate` returns ~1,000 shannons/kB.

**Attack:**

1. Attacker calls `send_transaction` RPC to submit a large batch of transactions with very high fee rates (e.g., 500,000 shannons/kB) referencing valid UTXOs. Each accepted transaction triggers `accept_tx`, appending high-weight, high-fee-rate entries to `self.txs[current_tip]`.

2. Attacker immediately submits conflicting transactions (double-spends) for the same inputs. The original high-fee transactions are evicted from the tx-pool. `reject_tx` is called but is a no-op for `WeightUnitsFlow` — `self.txs[current_tip]` retains all the inflated entries.

3. Any subsequent call to `estimate_fee_rate` within the next `2 × target_blocks` blocks computes `flow_speed_buckets` using the poisoned `self.txs` data. The `added_weight` term is inflated, causing the algorithm to recommend a fee rate far above the true market rate.

**Concrete numbers (default `HIGH_TARGET = 5` blocks):**
- `historical_blocks = 10`
- Attacker injects 10 MB of fake high-fee flow in one block
- `flow_speed = 10 MB / 10 blocks = 1 MB/block`
- `added_weight = 1 MB/block × 5 blocks = 5 MB`
- `removed_weight = MAX_BLOCK_BYTES × 85% × 5 ≈ 5.1 MB`
- Low-fee buckets fail the `passed` check; estimator recommends the highest available fee rate
- This persists for 10 blocks (~5 minutes) before the poisoned data expires [1](#0-0) [2](#0-1) [7](#0-6)

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L147-151)
```rust
    fn expire(&mut self) {
        let historical_blocks = Self::historical_blocks(constants::MAX_TARGET);
        let expired_tip = self.current_tip.saturating_sub(historical_blocks);
        self.txs.retain(|&num, _| num >= expired_tip);
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L153-162)
```rust
    pub fn accept_tx(&mut self, info: TxEntryInfo) {
        if self.current_tip == 0 {
            return;
        }
        let item = TxStatus::new_from_entry_info(info);
        self.txs
            .entry(self.current_tip)
            .and_modify(|items| items.push(item))
            .or_insert_with(|| vec![item]);
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L244-272)
```rust
        // Calculate flow speeds for buckets.
        let flow_speed_buckets = {
            let historical_tip = self.current_tip - historical_blocks;
            let sorted_flowed = self.sorted_flowed(historical_tip);
            let mut buckets = vec![0u64; max_bucket_index + 1];
            let mut index_curr = max_bucket_index;
            for tx in &sorted_flowed {
                let index = Self::max_bucket_index_by_fee_rate(tx.fee_rate);
                if index > max_bucket_index {
                    continue;
                }
                if index < index_curr {
                    let flowed_curr = buckets[index_curr];
                    for i in buckets.iter_mut().take(index_curr) {
                        *i = flowed_curr;
                    }
                }
                buckets[index] += tx.weight;
                index_curr = index;
            }
            let flowed_curr = buckets[index_curr];
            for i in buckets.iter_mut().take(index_curr) {
                *i = flowed_curr;
            }
            buckets
                .into_iter()
                .map(|value| value / historical_blocks)
                .collect::<Vec<_>>()
        };
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L279-297)
```rust
        for bucket_index in 1..=max_bucket_index {
            let current_weight = current_weight_buckets[bucket_index];
            let added_weight = flow_speed_buckets[bucket_index] * target_blocks;
            // Note: blocks are not full even there are many pending transactions,
            // since `MAX_BLOCK_PROPOSALS_LIMIT = 1500`.
            let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
            let passed = current_weight + added_weight <= removed_weight;
            ckb_logger::trace!(
                ">>> bucket[{}]: {}; {} + {} - {}",
                bucket_index,
                passed,
                current_weight,
                added_weight,
                removed_weight
            );
            if passed {
                let fee_rate = Self::lowest_fee_rate_by_bucket_index(bucket_index);
                return Ok(fee_rate);
            }
```

**File:** util/fee-estimator/src/estimator/mod.rs (L84-88)
```rust
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
```

**File:** util/fee-estimator/src/constants.rs (L10-24)
```rust
pub(crate) const MAX_TARGET: BlockNumber = (60 * 60) / AVG_BLOCK_INTERVAL;
/// Min target blocks, in next block (5).
/// NOTE After tests, 3 blocks are too strict; so to adjust larger: 5.
pub(crate) const MIN_TARGET: BlockNumber = (TX_PROPOSAL_WINDOW.closest() + 1) + 2;

/// Lowest fee rate.
pub(crate) const LOWEST_FEE_RATE: FeeRate = FeeRate::from_u64(1000);

/// Target blocks for no priority (lowest priority, about 1 hour, 128).
pub const DEFAULT_TARGET: BlockNumber = MAX_TARGET;
/// Target blocks for low priority (about 30 minutes, 64).
pub const LOW_TARGET: BlockNumber = DEFAULT_TARGET / 2;
/// Target blocks for medium priority (about 10 minutes, 42).
pub const MEDIUM_TARGET: BlockNumber = LOW_TARGET / 3;
/// Target blocks for high priority (3).
```
