### Title
`check_tx_fee` Assumes `weight = tx_size`, Ignoring Cycles Contribution — Allows Below-Minimum Effective Fee Rate Admission - (File: `tx-pool/src/util.rs`)

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate gate using only `tx_size` as the transaction weight. However, the actual weight used everywhere else in the pool is `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction sender can craft a cycle-heavy, byte-light transaction that passes the admission check with a fee far below what its actual weight warrants. The same flaw exists in `calculate_min_replace_fee` for RBF.

### Finding Description

`check_tx_fee` computes the minimum required fee as:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The actual transaction weight used for ordering, eviction, and fee-rate statistics is defined as:

```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

The same size-only assumption is present in `calculate_min_replace_fee` for RBF:

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [3](#0-2) 

The `TxEntry::fee_rate()` method and `AncestorsScoreSortKey` both use `get_transaction_weight` with cycles, so the pool's internal ordering correctly reflects actual weight — but the admission gate does not. [4](#0-3) 

### Impact Explanation

With `max_tx_verify_cycles = 70,000,000` (default) and `DEFAULT_BYTES_PER_CYCLES ≈ 0.000_170_571_4`:

- A transaction with `tx_size = 100` bytes and `cycles = 70,000,000` has:
  - `cycles_equivalent = 70,000,000 × 0.000_170_571_4 ≈ 11,940` bytes
  - `actual_weight = max(100, 11,940) = 11,940`
- The admission check requires: `min_fee_rate × 100 / 1000 = 100 shannons` (at default 1000 shannons/KW)
- The correct weight-based minimum would be: `1000 × 11,940 / 1000 = 11,940 shannons`
- **Discrepancy: ~119×**

An attacker can flood the mempool with cycle-heavy, byte-light transactions at ~1/119th of the intended minimum cost. These transactions:
1. Pass the admission gate
2. Consume full script verification resources (up to 70M cycles each)
3. Occupy pool slots with an effective fee rate far below `min_fee_rate`
4. Displace legitimate higher-fee-rate transactions when the pool fills

For RBF, the same flaw allows a cycle-heavy replacement transaction to satisfy the `min_rbf_rate` extra-fee requirement based on size alone, bypassing the intended fee bump threshold. [5](#0-4) 

### Likelihood Explanation

Any unprivileged transaction sender reachable via the `send_transaction` JSON-RPC or P2P relay can exploit this. Crafting a transaction with high declared cycles and small serialized size is straightforward — the attacker simply writes a script that consumes many cycles but requires few bytes to encode. No special privileges, keys, or majority hashpower are needed. [6](#0-5) 

### Recommendation

Replace the size-only weight in `check_tx_fee` with the actual transaction weight. Since cycles are not yet known at the pre-verification admission stage, the check should either:

1. Use `get_transaction_weight(tx_size, declared_cycles)` where `declared_cycles` is the caller-declared cycle count (already available in the submission path before full verification), or
2. Apply the min fee check a second time post-verification using the verified cycle count.

The same fix should be applied to `calculate_min_replace_fee`. [7](#0-6) 

### Proof of Concept

1. Craft a CKB transaction with a lock script that loops for ~70,000,000 cycles but whose serialized size is ~100 bytes.
2. Set the transaction fee to `ceil(min_fee_rate × 100 / 1000) + 1 = 101 shannons` (at default `min_fee_rate = 1000`).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 100 / 1000 = 100 shannons`; the transaction passes.
5. After admission, `TxEntry::fee_rate()` computes `FeeRate::calculate(101, 11940) ≈ 8 shannons/KW` — far below the 1000 shannons/KW minimum.
6. Repeat with many UTXOs to fill the pool with ~8 shannons/KW transactions at a cost of ~101 shannons each, instead of the intended ~11,940 shannons each. [4](#0-3) [2](#0-1)

### Citations

**File:** tx-pool/src/util.rs (L28-54)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
}
```

**File:** util/types/src/core/tx_pool.rs (L276-303)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

/// vbytes has been deprecated, renamed to weight to prevent ambiguity
#[deprecated(
    since = "0.107.0",
    note = "Please use the get_transaction_weight instead"
)]
pub fn get_transaction_virtual_bytes(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}

/// The miners select transactions to fill the limited block space which gives the highest fee.
/// Because there are two different limits, serialized size and consumed cycles,
/// the selection algorithm is a multi-dimensional knapsack problem.
/// Introducing the transaction weight converts the multi-dimensional knapsack to a typical knapsack problem,
/// which has a simple greedy algorithm.
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/pool.rs (L101-114)
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
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
