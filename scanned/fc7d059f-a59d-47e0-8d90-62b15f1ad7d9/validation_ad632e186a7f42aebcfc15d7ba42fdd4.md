### Title
Wrong Size Used in `min_replace_fee()` Advisory Estimate Causes RBF Replacement Transactions to Be Rejected - (File: tx-pool/src/pool.rs)

### Summary

`TxPool::min_replace_fee()` computes the advisory minimum replacement fee using the **old (existing) transaction's size**, while the actual enforcement in `check_rbf()` uses the **new (replacement) transaction's size**. When a replacement transaction differs in size from the original, the advisory value returned via the `get_transaction` RPC is incorrect — potentially too low — causing users who rely on it to submit replacement transactions that are rejected by the node.

### Finding Description

`TxPool::min_replace_fee()` is the function that computes the advisory `min_replace_fee` field returned by the `get_transaction` RPC (verbosity=2). It is called with the existing pool entry and passes `tx.size` — the old transaction's serialized size — to `calculate_min_replace_fee()`: [1](#0-0) 

Inside `calculate_min_replace_fee()`, the extra RBF fee component is computed as: [2](#0-1) 

However, the actual enforcement path in `check_rbf()` passes `entry.size` — the **new** replacement transaction's size — to the same function: [3](#0-2) 

The two call sites use different `size` values:

| Call site | `size` used | Purpose |
|---|---|---|
| `min_replace_fee()` | `tx.size` (old tx) | Advisory RPC field |
| `check_rbf()` | `entry.size` (new tx) | Actual enforcement |

When a user constructs a replacement transaction with a different size than the original (e.g., different outputs, witnesses, or cell deps), the advisory `min_replace_fee` is computed with the wrong size. If the replacement is larger than the original, the advisory value is **lower** than what `check_rbf()` will actually require, causing the replacement to be rejected even though the user paid what the advisory said was sufficient.

The `extra_rbf_fee` is computed via `FeeRate::fee()`: [4](#0-3) 

### Impact Explanation

**Impact: Low.** The actual enforcement logic in `check_rbf()` is correct and uses the right size. No funds are at risk and no consensus rule is violated. The only consequence is that users who rely on the `min_replace_fee` advisory field from the RPC to set their replacement transaction fee may have their replacement rejected when the replacement tx is larger than the original. They must retry with a higher fee. The discrepancy is bounded by `min_rbf_rate × |new_size − old_size|`, which is typically small (hundreds of shannons for typical size differences).

### Likelihood Explanation

**Likelihood: High.** Any RBF replacement transaction that differs in size from the original triggers this discrepancy. This is the common case: users replacing a transaction typically change outputs (e.g., to increase the fee by reducing output capacity), which changes the serialized size. The `min_replace_fee` field is specifically designed to guide users in setting the correct replacement fee, so users are expected to rely on it.

### Recommendation

In `min_replace_fee()`, the function cannot know the new tx's size in advance (it is called on the existing pool entry). The advisory should either:
1. Document clearly that the returned value assumes the replacement tx has the same size as the original, and users should add `min_rbf_rate × (new_size − old_size)` if the replacement is larger; or
2. Remove the `extra_rbf_fee` component from the advisory (since it depends on the unknown new tx's size) and only return `sum(replaced_txs.fee)`, letting users add the size-based component themselves.

### Proof of Concept

1. Submit tx1 (size S1) to the pool with RBF enabled (`min_rbf_rate > min_fee_rate`).
2. Call `get_transaction(tx1_hash, verbosity=2)` → observe `min_replace_fee = sum(tx1.fee) + min_rbf_rate * S1`.
3. Construct tx2 (replacement) with additional outputs so its size S2 > S1. Set tx2's fee = `min_replace_fee` from step 2.
4. Submit tx2 → rejected with `"Tx's current fee is X, expect it to >= Y to replace old txs"` where Y = `sum(tx1.fee) + min_rbf_rate * S2 > min_replace_fee`.

The rejection occurs because `check_rbf()` computes the threshold using S2 (new tx size) while the advisory used S1 (old tx size). [1](#0-0) [5](#0-4) [6](#0-5)

### Citations

**File:** tx-pool/src/pool.rs (L86-99)
```rust
    pub fn min_replace_fee(&self, tx: &TxEntry) -> Option<Capacity> {
        if !self.enable_rbf() {
            return None;
        }

        let mut conflicts = vec![self.get_pool_entry(&tx.proposal_short_id()).unwrap()];
        let descendants = self.pool_map.calc_descendants(&tx.proposal_short_id());
        let descendants = descendants
            .iter()
            .filter_map(|id| self.get_pool_entry(id))
            .collect::<Vec<_>>();
        conflicts.extend(descendants);
        self.calculate_min_replace_fee(&conflicts, tx.size)
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

**File:** util/types/src/core/fee_rate.rs (L33-37)
```rust
    /// Calculates the fee for a given weight.
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```
