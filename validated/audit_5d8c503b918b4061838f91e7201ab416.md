### Title
RBF Extra-Fee Computed on Serialized Size Instead of Transaction Weight, Allowing Cycle-Heavy Replacements to Underpay — (`File: tx-pool/src/pool.rs`)

---

### Summary

`calculate_min_replace_fee` computes the mandatory extra fee for Replace-By-Fee (RBF) using the raw serialized byte size of the incoming transaction, while every other fee-rate enforcement path in the tx-pool uses the **transaction weight** (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). For a cycle-heavy replacement transaction the weight can be orders of magnitude larger than the serialized size, so the required extra fee is severely underestimated and the RBF bump threshold is effectively bypassed.

---

### Finding Description

CKB's fee-rate metric is `weight = max(tx_size_bytes, cycles × 0.000_170_571_4)`. [1](#0-0) 

`TxEntry::fee_rate()` uses this weight: [2](#0-1) 

`FeeRate::fee()` multiplies the stored rate by whatever `weight` argument is passed: [3](#0-2) 

`calculate_min_replace_fee` passes only `size` (serialized bytes) as that argument: [4](#0-3) 

The call site in `check_rbf` (the actual enforcement gate) passes `entry.size` — the new transaction's byte count — not its weight: [5](#0-4) 

For a cycle-heavy transaction the discrepancy is large. With `max_tx_verify_cycles = 70 000 000` and a compact 100-byte transaction body:

```
weight  = max(100, 70_000_000 × 0.000_170_571_4) ≈ 11 940 bytes
extra_rbf_fee (actual)  = min_rbf_rate × 100  / 1000
extra_rbf_fee (correct) = min_rbf_rate × 11940 / 1000   (~119× larger)
```

The replacement transaction is admitted after paying only ~0.8 % of the intended extra fee.

---

### Impact Explanation

The RBF bump requirement (`min_replace_fee = Σ replaced_fees + extra_rbf_fee`) is the primary economic barrier against cheap mempool churn and transaction-pinning attacks. By crafting a cycle-heavy replacement transaction an unprivileged submitter can:

1. Replace an existing pool transaction while paying a negligible extra fee relative to the block resources the replacement actually consumes.
2. Repeatedly cycle replacements at near-zero marginal cost, exhausting miner bandwidth and degrading mempool quality without paying the fee rate the node operator configured.

The impact is confined to tx-pool admission policy (not consensus), but it directly undermines the RBF protection that node operators rely on.

**Impact: Medium**

---

### Likelihood Explanation

Any RPC/P2P caller with access to `send_transaction` can submit a transaction that declares a high cycle count. The CKB VM cycle limit (`max_tx_verify_cycles`) is enforced after admission, but the size/weight split is visible before verification. A script author can trivially produce a transaction whose declared (and actual) cycle consumption is near the maximum while keeping the serialized byte size small. No special privilege is required.

**Likelihood: Medium**

---

### Recommendation

Replace the raw `size` argument with the transaction weight in `calculate_min_replace_fee`:

```rust
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize, cycles: Cycle) -> Option<Capacity> {
    let weight = get_transaction_weight(size, cycles);
    let extra_rbf_fee = self.config.min_rbf_rate.fee(weight);
    ...
}
```

Update both call sites (`check_rbf` and `min_replace_fee`) to pass `entry.cycles` / `tx.cycles` alongside `entry.size` / `tx.size`. This aligns the RBF admission gate with the weight-based fee-rate metric used everywhere else in the pool.

---

### Proof of Concept

1. Node configured with `min_rbf_rate = 1500` (shannons/KW), `max_tx_verify_cycles = 70_000_000`.
2. Submit transaction T1 (100-byte body, fee = 10 000 shannons) → accepted.
3. Craft replacement T2: same inputs, 100-byte body, 70 000 000 cycles, fee = 10 150 shannons.
   - `extra_rbf_fee` computed by node: `1500 × 100 / 1000 = 150` shannons ✓ (passes check)
   - Correct `extra_rbf_fee`: `1500 × 11940 / 1000 = 17 910` shannons (T2 should be rejected)
4. T2 is accepted despite paying only 150 shannons extra instead of the required 17 910 shannons, consuming ~119× more block-cycle resources than the fee bump accounts for. [6](#0-5) [7](#0-6) [1](#0-0)

### Citations

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** tx-pool/src/pool.rs (L101-127)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
        // don't account for duplicate txs
        let replaced_fees: HashMap<_, _> = conflicts
            .iter()
            .map(|c| (c.id.clone(), c.inner.fee))
            .collect();
        let replaced_sum_fee = replaced_fees
            .values()
            .try_fold(Capacity::zero(), |acc, x| acc.safe_add(*x));
        let res = replaced_sum_fee.map_or(Err(CapacityError::Overflow), |sum| {
            sum.safe_add(extra_rbf_fee)
        });
        if let Ok(res) = res {
            Some(res)
        } else {
            let fees = conflicts.iter().map(|c| c.inner.fee).collect::<Vec<_>>();
            error!(
                "conflicts: {:?} replaced_sum_fee {:?} overflow by add {}",
                conflicts.iter().map(|e| e.id.clone()).collect::<Vec<_>>(),
                fees,
                extra_rbf_fee
            );
            None
        }
    }
```

**File:** tx-pool/src/pool.rs (L662-676)
```rust
        // Rule #4, new tx's fee need to higher than min_rbf_fee computed from the tx_pool configuration
        // Rule #3, new tx's fee need to higher than conflicts, here we only check the all conflicted txs fee
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
        } else {
            return Err(Reject::RBFRejected(
                "calculate_min_replace_fee failed".to_string(),
            ));
        }
```
