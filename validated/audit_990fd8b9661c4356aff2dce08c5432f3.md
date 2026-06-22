### Title
Fee Rate Minimum Check Uses Transaction Size Instead of Weight, Bypassing Cycle-Weighted Admission Control - (`tx-pool/src/util.rs`)

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using raw serialized byte size (`tx_size`) rather than the canonical transaction weight (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). The code itself acknowledges this is theoretically incorrect. Because cycles are known only after script execution and no weight-based fee check is performed post-execution, a tx-pool submitter can craft a transaction with maximum cycles and minimal size that passes the size-based gate with a fee far below what the weight-based `min_fee_rate` would require.

### Finding Description

`FeeRate` is defined as **shannons per kilo-weight**, where weight is computed by `get_transaction_weight`: [1](#0-0) 

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, so a transaction consuming 70 000 000 cycles has a weight of ≈ 11 940 weight-units regardless of its byte size. [2](#0-1) 

The sole admission-time fee gate is `check_tx_fee`: [3](#0-2) 

```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,          // ← byte size, not weight
) -> Result<Capacity, Reject> {
    ...
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    if fee < min_fee {
        return Err(Reject::LowFeeRate(...));
    }
    Ok(fee)
}
```

The comment explicitly concedes the theoretical incorrectness. `check_tx_fee` is called **before** script execution, when cycles are not yet known. After script execution the cycles are recorded in the `TxEntry`, but no subsequent weight-based fee rate check is performed; the entry is inserted directly into the pool. `TxEntry::fee_rate()` correctly uses weight for **sorting/eviction**, but that is not a rejection gate: [4](#0-3) 

The same unit mismatch appears in the RBF path. `calculate_min_replace_fee` computes the extra fee required for replacement using the replacement transaction's byte size, not its weight: [5](#0-4) 

```rust
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
    ...
}
```

### Impact Explanation

With `min_fee_rate = 1 000` shannons/KW (the default) and a transaction of 200 bytes consuming 70 000 000 cycles:

| Metric | Value |
|---|---|
| Actual weight | `max(200, 70 000 000 × 0.000170571)` ≈ **11 940** |
| `min_fee` via size check | `1 000 × 200 / 1 000` = **200 shannons** |
| `min_fee` via weight | `1 000 × 11 940 / 1 000` = **11 940 shannons** |

A transaction paying only 200 shannons passes the gate despite having an effective fee rate of `200 × 1000 / 11940 ≈ 16 shannons/KW` — roughly **60× below** the configured threshold. An attacker can flood the tx-pool with cycle-heavy, byte-light transactions at a fraction of the intended cost, exhausting pool capacity and degrading node performance for legitimate users. The same arithmetic applies to RBF: a high-cycle replacement transaction can displace existing pool entries while paying far less than the `min_rbf_rate` threshold requires.

### Likelihood Explanation

The attack requires only an unprivileged RPC call to `send_transaction`. No special role, key, or majority hashpower is needed. The attacker controls both the transaction size (keep it small) and the script complexity (maximize cycles up to `max_tx_verify_cycles`, default 70 000 000). The discrepancy between size-based gate and weight-based reality is largest precisely at the maximum-cycles boundary, which is easy to target deliberately.

### Recommendation

After script execution, when cycles are known, perform a second fee rate check using the actual weight:

```rust
// After cycles are resolved:
let weight = get_transaction_weight(tx_size, cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

Apply the same correction to `calculate_min_replace_fee`, substituting `get_transaction_weight(size, entry.cycles)` for the raw `size` argument passed to `min_rbf_rate.fee(...)`.

### Proof of Concept

1. Craft a CKB transaction whose lock/type script runs a tight loop consuming ≈ 70 000 000 cycles but whose serialized size is ≈ 200 bytes (achievable with a single input/output and a compact witness).
2. Set the fee to 201 shannons (just above the size-based threshold of 200 shannons).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`; the transaction passes.
5. Script execution consumes 70 000 000 cycles; the entry is inserted with weight ≈ 11 940.
6. The effective fee rate stored in the pool is `201 × 1000 / 11940 ≈ 16 shannons/KW`, far below the 1 000 shannons/KW threshold.
7. Repeat to fill the pool with cycle-expensive, fee-cheap transactions, crowding out legitimate higher-fee-rate transactions and wasting node verification resources. [6](#0-5) [7](#0-6) [5](#0-4)

### Citations

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

**File:** tx-pool/src/util.rs (L28-53)
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
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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
