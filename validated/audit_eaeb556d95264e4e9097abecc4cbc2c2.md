### Title
Fee-Rate Admission Uses Size-Only Metric While Pool Stores Weight-Based Rate, Allowing Sub-Minimum-Fee-Rate Transactions Into the Pool - (File: tx-pool/src/util.rs)

### Summary

The tx-pool admission gate in `check_tx_fee` enforces `min_fee_rate` using raw serialized byte size as the denominator, but the pool entry's actual fee rate (`TxEntry::fee_rate()`) is computed using `get_transaction_weight(size, cycles)` — `max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For any transaction where cycles dominate (i.e., `cycles * 0.000_170_571_4 > size`), the admission check passes with a fee that satisfies the size-only threshold, yet the transaction's effective fee rate stored in the pool is far below `min_fee_rate`. This is the direct CKB analog of the Gator currency mismatch: one metric is used for the gate decision, a different metric is used for the actual value stored and acted upon.

### Finding Description

**Admission gate** (`tx-pool/src/util.rs`, `check_tx_fee`):

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

The gate computes `min_fee = min_fee_rate * tx_size / 1000`. [1](#0-0) 

**Pool entry fee rate** (`tx-pool/src/component/entry.rs`, `TxEntry::fee_rate`):

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

The stored fee rate uses `weight = max(size, cycles * 0.000_170_571_4)`. [2](#0-1) 

**Weight formula** (`util/types/src/core/tx_pool.rs`):

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

The `check_tx_fee` is called in `pre_check` before script execution, so cycles are not yet known at admission time. [4](#0-3)  After verification completes and actual cycles are known, the `TxEntry` is created with those cycles, but **no second fee-rate check is performed against `min_fee_rate` using the weight-based metric**.

**Concrete mismatch example:**
- `min_fee_rate = 1000 shannons/KW` (default mainnet config)
- `tx_size = 1000 bytes`, `cycles = 100,000,000`
- `weight = max(1000, 100_000_000 × 0.000_170_571_4) = 17,057`
- Admission requires: `fee ≥ 1000 × 1000 / 1000 = 1000 shannons` ✓ passes with fee=1000
- Actual pool fee rate: `1000 × 1000 / 17057 ≈ 58 shannons/KW` — **17× below `min_fee_rate`**

### Impact Explanation

1. **Pool admission bypass**: Transactions with high cycles relative to size pass the `min_fee_rate` gate with fees that are a fraction of what the weight-based policy would require. The pool accepts transactions whose effective fee rate is far below the configured minimum.

2. **Fee estimation skew**: The `estimate_fee_rate` RPC and the `weight_units_flow` fee estimator both use weight-based fee rates. [5](#0-4)  Admitted sub-minimum-rate transactions pollute the fee estimator's historical data, causing it to recommend lower fee rates to users.

3. **Pool statistics mismatch**: `tx_pool_info` reports `min_fee_rate` as the admission threshold, but the pool contains transactions with effective fee rates far below it, misleading RPC callers about actual pool state. [6](#0-5) 

4. **Eviction ordering distortion**: Eviction uses `EvictKey` which is weight-based. [7](#0-6)  High-cycles transactions admitted via the size-only gate will have very low eviction keys, causing them to be evicted first — but until eviction, they occupy pool slots that legitimate transactions cannot fill.

### Likelihood Explanation

**Moderate.** Any unprivileged `send_transaction` RPC caller can submit a transaction. The attacker needs a script that consumes many cycles but produces a small serialized transaction. CKB-VM scripts can be arbitrarily cycle-intensive (e.g., tight loops in a lock script) while the transaction itself remains small. The `max_tx_verify_cycles` limit (default 70,000,000) bounds the maximum cycle count per transaction, but at 70M cycles and 1000 bytes size, the weight is `max(1000, 70_000_000 × 0.000_170_571_4) = 11,940`, so the admission check underestimates the required fee by ~12×. This is reachable on any node with the default configuration.

### Recommendation

After script verification completes and actual cycles are known, perform a second fee-rate check using the weight-based metric before inserting the entry into the pool:

```rust
let weight = get_transaction_weight(entry.size, entry.cycles);
let actual_fee_rate = FeeRate::calculate(entry.fee, weight);
if actual_fee_rate < tx_pool.config.min_fee_rate {
    return Err(Reject::LowFeeRate(...));
}
```

Alternatively, if the size-only pre-check is intentionally kept as a fast path, document clearly that `min_fee_rate` is enforced on size only (not weight), and adjust the `tx_pool_info` response and documentation to reflect this. The fee estimator and pool statistics should also be made consistent with whichever metric is authoritative.

### Proof of Concept

1. Configure a CKB node with default `min_fee_rate = 1000` shannons/KW.
2. Deploy a lock script that runs a tight loop consuming ~70,000,000 cycles.
3. Construct a transaction spending a cell locked by that script. Serialized size ≈ 1,000 bytes.
4. Set the transaction fee to exactly `1,000 shannons` (satisfies `min_fee_rate * size / 1000 = 1000`).
5. Submit via `send_transaction` RPC.
6. The transaction is admitted. Its actual weight = `max(1000, 70_000_000 × 0.000_170_571_4) ≈ 11,940`.
7. Actual fee rate = `1000 × 1000 / 11940 ≈ 83 shannons/KW` — **12× below `min_fee_rate`**.
8. Observe via `get_raw_tx_pool` (verbose=true) that the transaction is in the pool. The `weight` field in `AncestorsScoreSortKey` reflects the true weight, confirming the mismatch with the admission threshold. [8](#0-7) [2](#0-1) [9](#0-8)

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

**File:** util/types/src/core/tx_pool.rs (L339-343)
```rust
    /// Fee rate threshold. The pool rejects transactions which fee rate is below this threshold.
    ///
    /// The unit is Shannons per 1000 bytes transaction serialization size in the block.
    pub min_fee_rate: FeeRate,

```

**File:** tx-pool/src/process.rs (L274-290)
```rust
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L97-101)
```rust
    fn new_from_entry_info(info: TxEntryInfo) -> Self {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        Self { weight, fee_rate }
    }
```
