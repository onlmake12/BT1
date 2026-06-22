### Title
`FeeRate::calculate` Silent Saturation Corrupts Fee-Rate Ordering and RPC Statistics for High-Fee Transactions - (File: `util/types/src/core/fee_rate.rs`)

---

### Summary

`FeeRate::calculate` uses `saturating_mul` when scaling the fee by `KW = 1000`. For any transaction whose fee exceeds `u64::MAX / 1000` shannons (~184 million CKB), the intermediate product silently saturates to `u64::MAX`, producing a computed fee rate that is **lower** than the true value. This incorrect fee rate propagates into tx-pool eviction ordering, the `get_fee_rate_statistics` / `get_fee_rate_statics` RPC, and the `estimate_fee_rate` RPC, all reachable by an unprivileged `send_transaction` caller.

---

### Finding Description

In `util/types/src/core/fee_rate.rs`, `FeeRate::calculate` computes:

```rust
FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
```

`KW = 1000`. When `fee.as_u64() > u64::MAX / 1000 ≈ 1.844 × 10¹⁶` shannons, `saturating_mul` clamps the product to `u64::MAX` and the returned fee rate is `u64::MAX / weight` instead of the mathematically correct `fee * 1000 / weight`. Because `u64::MAX / weight < fee * 1000 / weight` in this regime, the computed fee rate is **strictly lower** than the true rate. [1](#0-0) 

The same pattern appears in `FeeRate::fee`:

```rust
let fee = self.0.saturating_mul(weight) / KW;
``` [2](#0-1) 

The incorrect fee rate produced by `FeeRate::calculate` flows into every consumer:

**1. Tx-pool eviction key** (`tx-pool/src/component/entry.rs`): `EvictKey` is built from `FeeRate::calculate(entry.descendants_fee, descendants_weight)` and `FeeRate::calculate(entry.fee, weight)`. The pool evicts the entry with the *lowest* `EvictKey` first. A high-fee transaction whose rate is saturated downward may be evicted before lower-fee transactions. [3](#0-2) 

**2. `limit_size` eviction loop** (`tx-pool/src/pool.rs`): calls `next_evict_entry` which is ordered by `EvictKey`, so the corrupted rate directly determines which transactions are dropped when the pool is full. [4](#0-3) 

**3. `get_fee_rate_statistics` / `get_fee_rate_statics` RPC** (`rpc/src/util/fee_rate.rs`): pushes `FeeRate::calculate(fee, weight).as_u64()` into the statistics array. The mean and median returned to callers are incorrect for any block containing a saturating transaction. [5](#0-4) 

**4. Fallback `estimate_fee_rate`** (`tx-pool/src/component/pool_map.rs`): iterates entries by score and returns `entry.inner.fee_rate()`, which calls `FeeRate::calculate`. The estimate is incorrect for any high-fee entry that crosses the saturation threshold. [6](#0-5) 

**5. `ConfirmationFraction` and `WeightUnitsFlow` fee estimators** both call `FeeRate::calculate` when ingesting a new transaction, corrupting their internal tracking state. [7](#0-6) [8](#0-7) 

Notably, the developers already identified the overflow risk in fee comparisons and used `u128` arithmetic in `AncestorsScoreSortKey::min_fee_and_weight` and `Ord for AncestorsScoreSortKey` to avoid it — but did not apply the same fix to `FeeRate::calculate`. [9](#0-8) [10](#0-9) 

---

### Impact Explanation

An unprivileged `send_transaction` RPC caller who submits a transaction with fee > `u64::MAX / 1000` shannons (~184 million CKB) causes:

1. **Incorrect pool eviction**: The transaction's `EvictKey` fee rate is computed as `u64::MAX / weight` instead of the true (higher) value. When the pool is full, this transaction may be evicted before lower-fee transactions, violating the fee-priority invariant.
2. **Incorrect `get_fee_rate_statistics` output**: The RPC returns a mean/median that is lower than the true network fee rate, misleading wallets and users into setting insufficient fees for their own transactions.
3. **Incorrect `estimate_fee_rate` output**: The fallback estimator and both algorithmic estimators produce incorrect fee rate recommendations.

The overflow is silent — no error is returned, no log is emitted, and the node continues operating with corrupted fee-rate state.

---

### Likelihood Explanation

The saturation threshold is `u64::MAX / 1000 ≈ 1.844 × 10¹⁶` shannons ≈ 184 million CKB. The total initial CKB issuance is ~33.6 billion CKB, so this threshold represents roughly 0.55% of total supply. Controlling this amount is a high bar, making exploitation unlikely under normal economic conditions. However, the vulnerability is structurally reachable by any `send_transaction` caller with sufficient funds, requires no privileged access, and the code path is unconditionally exercised for every transaction entering the pool.

---

### Recommendation

Replace `saturating_mul` with a `u128`-widened intermediate, consistent with the pattern already used in `AncestorsScoreSortKey`:

```rust
// FeeRate::calculate
pub fn calculate(fee: Capacity, weight: u64) -> Self {
    if weight == 0 {
        return FeeRate::zero();
    }
    let rate = (fee.as_u64() as u128 * KW as u128 / weight as u128)
        .min(u64::MAX as u128) as u64;
    FeeRate::from_u64(rate)
}

// FeeRate::fee
pub fn fee(self, weight: u64) -> Capacity {
    let fee = (self.0 as u128 * weight as u128 / KW as u128)
        .min(u64::MAX as u128) as u64;
    Capacity::shannons(fee)
}
```

This preserves the correct value for all representable fee rates and only clamps at the true `u64::MAX` boundary.

---

### Proof of Concept

1. Construct a transaction whose inputs hold `2 × 10¹⁶` shannons more than its outputs (fee = 200 million CKB).
2. Submit via `send_transaction` RPC.
3. Observe the pool entry's evict key:
   - True fee rate: `2 × 10¹⁶ × 1000 / weight = 2 × 10¹⁹ / weight`
   - Computed fee rate: `u64::MAX / weight ≈ 1.844 × 10¹⁹ / weight`
   - The computed rate is ~8% lower than the true rate.
4. Fill the pool with transactions whose fee rates fall between the saturated value and the true value. The high-fee transaction is evicted first despite paying more.
5. Call `get_fee_rate_statistics` after the block containing this transaction is confirmed; the returned mean is lower than the true network fee rate. [1](#0-0) [11](#0-10) [12](#0-11)

### Citations

**File:** util/types/src/core/fee_rate.rs (L11-16)
```rust
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
    }
```

**File:** util/types/src/core/fee_rate.rs (L34-36)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
    }
```

**File:** tx-pool/src/pool.rs (L292-326)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
```

**File:** rpc/src/util/fee_rate.rs (L103-106)
```rust
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
```

**File:** tx-pool/src/component/pool_map.rs (L334-358)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L469-472)
```rust
    pub fn accept_tx(&mut self, tx_hash: Byte32, info: TxEntryInfo) {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        self.track_tx(tx_hash, fee_rate, self.current_tip)
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L97-100)
```rust
    fn new_from_entry_info(info: TxEntryInfo) -> Self {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        Self { weight, fee_rate }
```

**File:** tx-pool/src/component/sort_key.rs (L17-27)
```rust
    pub(crate) fn min_fee_and_weight(&self) -> (Capacity, u64) {
        // avoid division a_fee/a_weight > b_fee/b_weight
        let tx_weight = u128::from(self.fee.as_u64()) * u128::from(self.ancestors_weight);
        let ancestors_weight = u128::from(self.ancestors_fee.as_u64()) * u128::from(self.weight);

        if tx_weight < ancestors_weight {
            (self.fee, self.weight)
        } else {
            (self.ancestors_fee, self.ancestors_weight)
        }
    }
```

**File:** tx-pool/src/component/sort_key.rs (L36-49)
```rust
impl Ord for AncestorsScoreSortKey {
    fn cmp(&self, other: &Self) -> Ordering {
        // avoid division a_fee/a_weight > b_fee/b_weight
        let (fee, weight) = self.min_fee_and_weight();
        let (other_fee, other_weight) = other.min_fee_and_weight();
        let self_weight = u128::from(fee.as_u64()) * u128::from(other_weight);
        let other_weight = u128::from(other_fee.as_u64()) * u128::from(weight);
        if self_weight == other_weight {
            // if fee rate weight is same, then compare with ancestor weight
            self.ancestors_weight.cmp(&other.ancestors_weight)
        } else {
            self_weight.cmp(&other_weight)
        }
    }
```

**File:** rpc/src/module/chain.rs (L2124-2132)
```rust
    fn get_fee_rate_statics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }

    fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }
```
