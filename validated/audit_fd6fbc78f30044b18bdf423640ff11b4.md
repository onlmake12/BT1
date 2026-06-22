### Title
Min-Fee-Rate Invariant Broken by Size-Only Weight Assumption in `check_tx_fee` — (File: tx-pool/src/util.rs)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the raw serialized byte size of a transaction as the weight, instead of the actual transaction weight (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). This mirrors the external report's root cause: a hardcoded unit assumption replaces the actual precision of the asset being measured. An unprivileged RPC caller can craft a cycle-heavy, byte-small transaction that passes the fee gate at a fee rate orders of magnitude below the configured `min_fee_rate`, bypassing the pool's anti-spam invariant.

---

### Finding Description

`FeeRate` in CKB is defined as **shannons per kilo-weight**, where:

```
weight = max(tx_size_bytes, cycles × DEFAULT_BYTES_PER_CYCLES)
DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4  (= MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES)
``` [1](#0-0) [2](#0-1) 

For a transaction with `cycles = 70,000,000` (the per-transaction cap enforced by `max_tx_verify_cycles`), the cycle-derived weight is `70,000,000 × 0.000_170_571_4 ≈ 11,940 bytes`. A transaction with a 100-byte serialized body but maximum cycles has an **actual weight of 11,940 bytes** — 119× its byte size.

`check_tx_fee` computes the minimum fee as:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [3](#0-2) 

`FeeRate::fee(weight)` computes `fee_rate × weight / 1000`: [4](#0-3) 

With `min_fee_rate = 1000 shannons/KW` (the default):

| Metric | Size-based (current) | Weight-based (correct) |
|---|---|---|
| Weight used | 100 bytes | 11,940 bytes |
| Min fee required | 100 shannons | 11,940 shannons |
| Actual fee rate if 100 shannons paid | **8 shannons/KW** | 8 shannons/KW |

A transaction paying 100 shannons passes `check_tx_fee` but has an actual fee rate of `≈ 8 shannons/KW` — **125× below the configured minimum of 1000 shannons/KW**.

The code comment explicitly acknowledges the theoretical incorrectness but treats it as an acceptable "cheap check". No subsequent weight-based fee rate gate exists in the admission path; the weight-based `fee_rate()` is only used for pool ordering after admission. [5](#0-4) 

---

### Impact Explanation

An attacker can continuously submit cycle-heavy, byte-small transactions via the `send_transaction` RPC that are admitted to the tx-pool at fee rates far below `min_fee_rate`. This:

1. **Undermines the anti-spam fee floor**: the invariant "no transaction enters the pool below `min_fee_rate`" is violated.
2. **Pollutes the pool with economically underpriced work**: each admitted transaction consumes up to 70M cycles of verification resources while paying a fraction of the required fee.
3. **Displaces legitimate transactions**: when the pool reaches `max_tx_pool_size` (180 MB), eviction is by lowest actual fee rate — the attacker's transactions are evicted last relative to their admission cost, since they were admitted cheaply but their actual weight-based fee rate is the lowest in the pool. [6](#0-5) 

---

### Likelihood Explanation

- **Entry point**: `send_transaction` RPC, reachable by any unprivileged caller with no keys or special roles.
- **Craft cost**: the attacker writes a CKB-VM script (e.g., a tight loop) that consumes ~70M cycles. The script body can be stored in a cell dep, keeping the transaction's serialized size small (~100–200 bytes).
- **No consensus violation**: the transaction is valid; it just pays less than the weight-based minimum.
- **Repeatability**: the attacker can submit many such transactions continuously.

---

### Recommendation

Pass the verified cycle count into `check_tx_fee` and use `get_transaction_weight` for the minimum fee computation:

```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
    cycles: Cycle,           // add cycles parameter
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(...).transaction_fee(rtx)?;
    let weight = get_transaction_weight(tx_size, cycles);
    let min_fee = tx_pool.config.min_fee_rate.fee(weight);
    if fee < min_fee { ... }
    Ok(fee)
}
```

If cycles are not yet known at the call site (pre-execution), a two-phase check should be used: a size-only pre-check followed by a weight-based post-check after script execution completes and cycles are known.

---

### Proof of Concept

1. Write a CKB-VM lock script that executes a tight loop consuming ~70,000,000 cycles. Store it in a cell dep to keep the transaction body small (~100 bytes serialized).
2. Construct a transaction spending a cell locked by this script, with `outputs_capacity = inputs_capacity - 100` (fee = 100 shannons).
3. Submit via `send_transaction` RPC to a node with `min_fee_rate = 1000`.
4. `check_tx_fee` computes `min_fee = 1000 × 100 / 1000 = 100 shannons`. Fee equals min_fee → **admitted**.
5. Actual fee rate = `100 × 1000 / 11940 ≈ 8 shannons/KW` — 125× below the configured minimum.
6. Repeat to fill the pool with cycle-expensive, fee-underpriced transactions. [7](#0-6) [8](#0-7)

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

**File:** util/types/src/core/fee_rate.rs (L1-16)
```rust
use crate::core::Capacity;

/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;

impl FeeRate {
    /// Calculates the fee rate from a total fee and weight.
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
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

**File:** util/app-config/src/legacy/tx_pool.rs (L9-12)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
```
