### Title
Tx-Pool Minimum Fee Check Uses Raw Size Instead of Weight, Allowing Cycle-Heavy Transactions to Bypass `min_fee_rate` — (`tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` computes the minimum required fee using raw `tx_size` (bytes) as the weight denominator. However, `FeeRate` is defined as **shannons per kilo-weight**, where `weight = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Every other fee-rate calculation in the codebase — fee estimators, pool sorting, eviction, and block-level statistics — uses `get_transaction_weight(size, cycles)`. The admission gate uses a strictly weaker (unadjusted) metric, creating the same class of inconsistency as the reference report: one value is used to compute a quantity, but a different (unadjusted) value is used to gate it.

---

### Finding Description

**Root cause — `tx-pool/src/util.rs`, `check_tx_fee`, line 45:**

```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(...).transaction_fee(rtx)?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);  // ← uses size, not weight
    if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
    Ok(fee)
}
``` [1](#0-0) 

`FeeRate::fee(weight)` computes `self.0 * weight / 1000` — it expects a **weight** argument, not a raw byte count. [2](#0-1) 

**The adjusted value — `get_transaction_weight`:**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
// DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4
``` [3](#0-2) 

Every other fee-rate site uses this adjusted weight:

- `TxEntry::fee_rate()` — used for pool sorting and eviction: `FeeRate::calculate(self.fee, get_transaction_weight(self.size, self.cycles))` [4](#0-3) 
- `AncestorsScoreSortKey::from(&TxEntry)` — block-template selection score: `get_transaction_weight(entry.size, entry.cycles)` [5](#0-4) 
- `FeeRateCollector::statistics` — `get_fee_rate_statistics` RPC: `get_transaction_weight(*size, cycles)` [6](#0-5) 
- `ConfirmationFraction::accept_tx` and `WeightUnitsFlow::accept_tx` — fee estimators: `get_transaction_weight(info.size, info.cycles)` [7](#0-6) 

**The inconsistency in concrete numbers:**

With `min_fee_rate = 1000 shannons/KW` (the default) and a transaction of `size = 100 bytes`, `cycles = 70_000_000` (the default `max_tx_verify_cycles`):

| Metric | Value |
|---|---|
| `weight = max(100, 70_000_000 × 0.000_170_571_4)` | **11,940** |
| `min_fee` (size-based, what `check_tx_fee` enforces) | **100 shannons** |
| `min_fee` (weight-based, what `min_fee_rate` intends) | **11,940 shannons** |
| Ratio | **~119×** | [8](#0-7) 

---

### Impact Explanation

An unprivileged transaction sender can submit a cycle-heavy, byte-light transaction paying only the size-based minimum fee (e.g., 100 shannons) while the weight-based minimum fee rate would require ~11,940 shannons. The transaction passes `check_tx_fee` and is admitted to the pool. The attacker can fill the 180 MB tx-pool with such transactions at roughly 1/119th the intended cost, exhausting pool capacity and evicting legitimate transactions. The `min_fee_rate` configuration — the primary economic spam-prevention mechanism — is effectively bypassed for cycle-heavy transactions. [9](#0-8) 

---

### Likelihood Explanation

The attack requires only the ability to submit a transaction via the `send_transaction` RPC or P2P relay — no privileged access, no key material, no majority hashpower. Any node on the network is a valid target. The attacker needs to craft a transaction that consumes many cycles (e.g., a script with a tight loop) while keeping the serialized size small. This is straightforward given that CKB-VM scripts are arbitrary RISC-V programs. The default `max_tx_verify_cycles = 70_000_000` provides a large amplification factor. [10](#0-9) 

---

### Recommendation

After `verify_rtx` returns the actual cycles, perform a second fee-rate check using the true weight:

```rust
// After verify_rtx returns `completed` with actual cycles:
let weight = get_transaction_weight(tx_size, completed.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if completed.fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

The pre-execution size-based check can remain as a fast early-rejection gate, but a post-execution weight-based check must be added to enforce the actual `min_fee_rate` invariant. [11](#0-10) 

---

### Proof of Concept

1. Craft a CKB transaction whose lock or type script contains a tight RISC-V loop consuming ~70,000,000 cycles, with a serialized size of ~100 bytes.
2. Compute `min_fee_size = min_fee_rate * size / 1000 = 1000 * 100 / 1000 = 100 shannons`.
3. Set the transaction fee to exactly 100 shannons (inputs capacity − outputs capacity = 100).
4. Submit via `send_transaction` RPC.
5. Observe the transaction is accepted into the pool despite its actual weight-based fee rate being `1000 * 100 / 11940 ≈ 8 shannons/KW` — far below the configured `min_fee_rate = 1000 shannons/KW`.
6. Repeat to fill the pool at ~1/119th the intended cost per unit of cycle-weight consumed. [11](#0-10) [8](#0-7)

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

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L221-231)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
    }
```

**File:** rpc/src/util/fee_rate.rs (L103-105)
```rust
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L469-472)
```rust
    pub fn accept_tx(&mut self, tx_hash: Byte32, info: TxEntryInfo) {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        self.track_tx(tx_hash, fee_rate, self.current_tip)
```

**File:** util/app-config/src/configs/tx_pool.rs (L11-43)
```rust
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
    /// txs need to pay larger fee rate than this for RBF
    #[serde(with = "FeeRateDef")]
    pub min_rbf_rate: FeeRate,
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
    #[serde(default = "default_max_tx_verify_workers")]
    pub max_tx_verify_workers: usize,
    /// max ancestors size limit for a single tx
    pub max_ancestors_count: usize,
    /// rejected tx time to live by days
    pub keep_rejected_tx_hashes_days: u8,
    /// rejected tx count limit
    pub keep_rejected_tx_hashes_count: u64,
    /// The file to persist the tx pool on the disk when tx pool have been shutdown.
    ///
    /// By default, it is a subdirectory of 'tx-pool' subdirectory under the data directory.
    #[serde(default)]
    pub persisted_data: PathBuf,
    /// The recent reject record database directory path.
    ///
    /// By default, it is a subdirectory of 'tx-pool' subdirectory under the data directory.
    #[serde(default)]
    pub recent_reject: PathBuf,
    /// The expiration time for pool transactions in hours
    pub expiry_hours: u8,
}
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-16)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```
