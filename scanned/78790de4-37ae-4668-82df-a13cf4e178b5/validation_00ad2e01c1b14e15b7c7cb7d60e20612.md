### Title
`check_tx_fee` Enforces `min_fee_rate` Using Transaction Serialized Size Instead of Weight, Allowing Cycle-Heavy Transactions to Bypass the Fee Rate Floor — (File: tx-pool/src/util.rs)

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` enforces the `min_fee_rate` threshold using only the transaction's serialized byte size, while the rest of the fee-rate subsystem (fee statistics, fee estimation, tx ordering) uses `get_transaction_weight(size, cycles)` — the maximum of size and `cycles × DEFAULT_BYTES_PER_CYCLES`. An unprivileged tx-pool submitter can craft a transaction whose script consumes near-maximum cycles but whose serialized size is tiny, paying a fee that satisfies the size-based gate while its true weight-based fee rate is orders of magnitude below `min_fee_rate`. The code comment at the check site explicitly acknowledges the mismatch but treats it as acceptable; no subsequent weight-based gate exists after cycle verification.

### Finding Description

**Root cause — unit mismatch between enforcement and measurement**

`check_tx_fee` (called during tx-pool admission, before cycle verification completes) computes the minimum required fee as:

```rust
// tx-pool/src/util.rs  L45
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

`FeeRate::fee(weight)` is defined as `fee_rate * weight / 1000` (shannons per kilo-weight). Here `tx_size` (bytes) is passed as `weight`, so the gate is effectively `min_fee_rate × size / 1000`.

The actual fee rate used everywhere else is weight-based:

```rust
// util/types/src/core/tx_pool.rs  L298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (hardcoded as `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`).

For a cycle-heavy transaction:

| Parameter | Value |
|---|---|
| `tx_size` | 100 bytes |
| `cycles` | 70 000 000 (near `max_tx_verify_cycles`) |
| `weight` | max(100, 70 000 000 × 0.000 170 571 4) ≈ **11 940 bytes** |
| Fee required by `check_tx_fee` | 1 000 × 100 / 1 000 = **100 shannons** |
| Fee required for true `min_fee_rate` | 1 000 × 11 940 / 1 000 = **11 940 shannons** |
| Ratio | **~119×** below the intended floor |

After `check_tx_fee` passes, the tx is verified by the VM (cycles measured), a `TxEntry` is created with the real cycles, and `TxEntry::fee_rate()` computes the weight-based rate — but no rejection gate re-checks it against `min_fee_rate`. The code comment at L42–44 of `tx-pool/src/util.rs` explicitly acknowledges the limitation:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly"

No compensating weight-based check follows.

**Propagation into fee estimation**

`weight_units_flow::Algorithm::accept_tx` is called for every admitted transaction:

```rust
// util/fee-estimator/src/estimator/weight_units_flow.rs  L153-161
pub fn accept_tx(&mut self, info: TxEntryInfo) {
    ...
    let item = TxStatus::new_from_entry_info(info);  // uses get_transaction_weight internally
    self.txs.entry(self.current_tip)...push(item);
}
```

These low-fee-rate entries are sorted into fee buckets and used to estimate the fee rate required for inclusion. Flooding the pool with cycle-heavy, size-tiny, low-fee transactions shifts the bucket distribution downward, causing the estimator to recommend fee rates below what miners actually require.

### Impact Explanation

1. **Fee rate floor bypass**: Transactions with true weight-based fee rates up to ~119× below `min_fee_rate` are admitted to the tx pool, consuming pool space (up to 180 MB) and displacing legitimate transactions via eviction.
2. **Fee estimation pollution**: The `weight_units_flow` and `confirmation_fraction` estimators ingest these entries; their output (exposed via `estimate_fee_rate` RPC) is biased downward, causing honest users who follow the estimate to underpay and have transactions stall.
3. **Fee rate statistics skew**: `FeeRateCollector::statistics` (backing `get_fee_rate_statistics` RPC) uses weight-based rates from confirmed blocks, so it is not directly polluted — but the estimation path is.

### Likelihood Explanation

The attack requires only the ability to submit transactions to the tx pool (standard RPC access, no privilege). The attacker writes a CKB-VM script that burns cycles (e.g., a tight loop), keeps the serialized transaction small, and pays the minimum size-based fee. The `max_tx_verify_cycles = 70 000 000` cap bounds the per-transaction weight amplification to ~119× but does not prevent the attack. Submitting many such transactions is cheap because the fee per transaction is near-zero.

### Recommendation

After cycle verification and `TxEntry` construction, add a weight-based fee rate gate:

```rust
let weight = get_transaction_weight(tx_size, cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

Alternatively, document that `min_fee_rate` is intentionally size-based and add a separate `min_fee_rate_by_weight` configuration knob so operators can enforce the weight-based floor independently.

### Proof of Concept

1. Write a CKB lock script that executes a tight arithmetic loop consuming ≈ 70 000 000 cycles.
2. Construct a transaction using that script; keep the serialized size ≤ 200 bytes.
3. Set the output capacity so that `inputs_capacity − outputs_capacity = 200 shannons` (fee).
4. Submit via `send_transaction` RPC.
5. **`check_tx_fee` gate**: `min_fee = 1 000 × 200 / 1 000 = 200 shannons ≤ 200` → **passes**.
6. VM verifies the script; cycles ≈ 70 000 000; `TxEntry` is created.
7. `TxEntry::fee_rate()` = `200 × 1 000 / max(200, 11 940)` ≈ **16 shannons/KW** — 62× below `min_fee_rate = 1 000`.
8. No rejection occurs; the entry is admitted and fed to the fee estimator.
9. Repeat with many such transactions to shift the estimator's output downward. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** util/types/src/core/tx_pool.rs (L339-348)
```rust
    /// Fee rate threshold. The pool rejects transactions which fee rate is below this threshold.
    ///
    /// The unit is Shannons per 1000 bytes transaction serialization size in the block.
    pub min_fee_rate: FeeRate,

    /// Min RBF rate threshold. The pool reject RBF transactions which fee rate is below this threshold.
    /// if min_rbf_rate > min_fee_rate then RBF is enabled on the node.
    ///
    /// The unit is Shannons per 1000 bytes transaction serialization size in the block.
    pub min_rbf_rate: FeeRate,
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L96-101)
```rust
impl TxStatus {
    fn new_from_entry_info(info: TxEntryInfo) -> Self {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        Self { weight, fee_rate }
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L153-161)
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
```
