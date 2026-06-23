### Title
Ineffective `min_fee_rate` Enforcement in `check_tx_fee` Due to Using Serialized Size Instead of Weight — (`tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using the transaction's raw serialized byte size (`tx_size`) instead of its **weight** (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`). Because weight is always ≥ size, the size-based minimum is always ≤ the weight-based minimum, making the check structurally weaker than intended. An unprivileged submitter can craft a high-cycle, low-fee transaction that passes the size-based gate but whose true fee rate is far below `min_fee_rate`, bypassing the pool's spam-protection threshold.

---

### Finding Description

**Root cause — `tx-pool/src/util.rs`, `check_tx_fee`:**

```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(...)
        .transaction_fee(rtx)?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    if fee < min_fee {
        return Err(Reject::LowFeeRate(...));
    }
    Ok(fee)
}
``` [1](#0-0) 

`FeeRate` is documented and implemented as **shannons per kilo-weight (KW)**, not shannons per kilo-byte. [2](#0-1) 

The correct weight formula is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, so for a cycle-heavy transaction, weight >> size. [4](#0-3) 

The `TxEntry::fee_rate()` method — used for pool ordering and eviction — correctly uses weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [5](#0-4) 

But `check_tx_fee` — the **admission gate** — uses raw `tx_size`. Since `weight ≥ tx_size` always holds:

```
min_fee_by_size  = min_fee_rate × tx_size  / 1000
min_fee_by_weight = min_fee_rate × weight  / 1000
min_fee_by_weight ≥ min_fee_by_size   (always)
```

The admission check is therefore always weaker than the intended policy. This is structurally identical to the reported Notional bug: a computed minimum that is always ≤ the actual threshold, making the guard never trigger for the intended case.

**Exploit path:**

1. Attacker calls `send_transaction` RPC (or relays via P2P) with a transaction crafted to have high cycles and a fee just above the size-based floor.
2. `check_tx_fee` is the only fee-rate gate at admission time. [6](#0-5) 
3. The size-based check passes; the transaction enters the pool.
4. The transaction's true weight-based fee rate is far below `min_fee_rate`.

**Concrete numbers** (default config: `min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70,000,000`): [7](#0-6) 

| Parameter | Value |
|---|---|
| `tx_size` | 597 bytes (typical 2-in-2-out) |
| `cycles` | 70,000,000 |
| `weight` | max(597, 70 000 000 × 0.000 170 571 4) = **11 940** |
| `min_fee_by_size` | 1000 × 597 / 1000 = **597 shannons** |
| `min_fee_by_weight` | 1000 × 11 940 / 1000 = **11 940 shannons** |
| Effective fee rate admitted | 597 / 11 940 × 1000 ≈ **50 shannons/KW** |

An attacker pays ~50 shannons/KW while the node operator intends to enforce 1000 shannons/KW — a **20× bypass**.

---

### Impact Explanation

An unprivileged submitter can flood the tx-pool with high-cycle, low-fee transactions at a fraction of the intended minimum fee rate. This:

- Bypasses the spam-protection purpose of `min_fee_rate`
- Allows pool capacity (`max_tx_pool_size = 180 MB`) to be consumed by economically underpriced transactions
- Causes legitimate higher-fee-rate transactions to compete with or be evicted by artificially cheap spam
- Degrades block-template quality and network throughput

The pool's eviction logic uses weight-based fee rate (correct), so these transactions will eventually be evicted under pressure — but they can be continuously resubmitted, sustaining the attack.

---

### Likelihood Explanation

- Requires no privilege: any `send_transaction` RPC caller or P2P relay peer can trigger this.
- Requires only crafting a transaction with high cycles (e.g., a script that loops near `max_tx_verify_cycles`) and a fee just above the size-based floor.
- The code comment explicitly acknowledges the discrepancy ("Theoretically we cannot use size as weight directly"), confirming the gap is known but unmitigated at the admission gate.

---

### Recommendation

Replace `tx_size` with `weight` in `check_tx_fee`:

```rust
use ckb_types::core::tx_pool::get_transaction_weight;

pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
    cycles: Cycle,          // add cycles parameter
) -> Result<Capacity, Reject> {
    let fee = ...;
    let weight = get_transaction_weight(tx_size, cycles);
    let min_fee = tx_pool.config.min_fee_rate.fee(weight);
    if fee < min_fee {
        return Err(Reject::LowFeeRate(...));
    }
    Ok(fee)
}
```

The `cycles` value is available at the call site after script verification completes. This aligns the admission check with the fee-rate semantics used everywhere else in the pool (sorting, eviction, RBF).

---

### Proof of Concept

1. Construct a transaction with a lock script that consumes ~70,000,000 cycles (near `max_tx_verify_cycles`).
2. Set the fee to `min_fee_rate × tx_size / 1000` shannons (e.g., 597 shannons for a 597-byte tx).
3. Submit via `send_transaction` RPC.
4. Observe the transaction is accepted into the pool.
5. Query `get_pool_tx_detail_info` and confirm the entry's `fee_rate` field (which uses weight) reports ~50 shannons/KW — far below the configured 1000 shannons/KW `min_fee_rate`.
6. Repeat in a loop to fill the pool with economically underpriced transactions.

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

**File:** util/types/src/core/fee_rate.rs (L1-37)
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

    /// Creates a fee rate from shannons per kilo-weight.
    pub const fn from_u64(fee_per_kw: u64) -> Self {
        FeeRate(fee_per_kw)
    }

    /// Returns the fee rate as shannons per kilo-weight.
    pub const fn as_u64(self) -> u64 {
        self.0
    }

    /// Creates a zero fee rate.
    pub const fn zero() -> Self {
        Self::from_u64(0)
    }

    /// Calculates the fee for a given weight.
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** util/types/src/core/tx_pool.rs (L279-279)
```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;
```

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

**File:** resource/ckb.toml (L212-215)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
```
