### Title
Inconsistent Fee Rate Unit Assumptions Between Enforcement, Estimation, and Documentation — (`util/types/src/core/fee_rate.rs`, `tx-pool/src/util.rs`, `rpc/src/module/experiment.rs`)

---

### Summary

CKB's fee rate subsystem makes implicit and contradictory assumptions about the unit of `FeeRate` across three distinct layers: the type definition (shannons per kilo-**weight**), the `check_tx_fee` enforcement path (uses raw byte **size** as weight), and the `estimate_fee_rate` RPC documentation (claims shannons per kilo**byte**). These inconsistencies are analogous to the Chainlink decimal assumption bug: different components silently assume different units for the same quantity, causing the fee enforcement and fee estimation to diverge for cycle-heavy transactions.

---

### Finding Description

**Layer 1 — Type definition**

`FeeRate` is defined and displayed as "shannons per kilo-weight": [1](#0-0) [2](#0-1) 

`weight` is not the same as byte size. It is defined as:

```
weight = max(tx_size_bytes, cycles * DEFAULT_BYTES_PER_CYCLES)
``` [3](#0-2) 

**Layer 2 — Enforcement uses size, not weight**

`check_tx_fee` explicitly substitutes raw byte size for weight when computing the minimum fee: [4](#0-3) 

The comment acknowledges the substitution is theoretically incorrect but calls it a "cheap check." For a cycle-heavy transaction where `cycles * DEFAULT_BYTES_PER_CYCLES >> tx_size`, the actual weight is much larger than the size, so the minimum fee enforced is far lower than what the weight-based fee rate would imply.

**Layer 3 — RPC documents the wrong unit**

The `estimate_fee_rate` RPC return value is documented as "shannons per **kilobyte**": [5](#0-4) 

But the returned value is `FeeRate::as_u64()`, which is shannons per kilo-**weight**: [6](#0-5) 

The config file compounds this with a third inconsistent label: [7](#0-6) 

And `TxPoolInfo.min_fee_rate` is documented as "Shannons per 1000 bytes transaction serialization size": [8](#0-7) 

All three labels (shannons/KB, shannons/KW, shannons per 1000 bytes) are used interchangeably for the same `FeeRate(u64)` value, but they are only equivalent when `weight == size`, which is not guaranteed.

---

### Impact Explanation

A transaction sender who calls `estimate_fee_rate` and interprets the result as "shannons per kilobyte" (as documented) will compute their fee as `fee_rate * size_in_KB`. For a cycle-heavy transaction where `weight >> size`, the actual fee rate enforced by the network (via `check_tx_fee`, which uses size) is lower than the weight-based rate returned by `estimate_fee_rate`. This means:

1. **Overpayment**: Senders of cycle-heavy transactions overpay fees because they apply a weight-based rate to their size, yielding a fee higher than the size-based minimum.
2. **Underpayment risk in the reverse direction**: A sender who correctly understands the unit as shannons/KW and applies it to weight may compute a fee that is correct for weight-based enforcement — but if a future node or relay enforces using weight (consistent with the type definition), the transaction would be rejected by nodes that enforce differently.
3. **Fee estimation divergence**: The `estimate_fee_rate` fallback algorithm (`pool_map.estimate_fee_rate`) and the `WeightUnitsFlow`/`ConfirmationFraction` estimators all compute rates using `get_transaction_weight` (weight-based), while `check_tx_fee` enforces using size. The returned estimate is not directly comparable to the enforced threshold for cycle-heavy transactions. [9](#0-8) [10](#0-9) 

---

### Likelihood Explanation

Any unprivileged RPC caller invoking `estimate_fee_rate` is affected. The RPC is publicly documented and intended for wallets and transaction senders to determine appropriate fees. The inconsistency is always present whenever a transaction's cycle-equivalent weight exceeds its byte size (i.e., whenever `cycles * 0.000_170_571_4 > tx_size`). This is a realistic condition for any script-heavy transaction.

---

### Recommendation

1. Standardize the unit of `FeeRate` across all documentation, config comments, and RPC return descriptions. Choose one canonical unit (shannons per kilo-weight) and apply it consistently everywhere.
2. In `check_tx_fee`, use `get_transaction_weight(tx_size, cycles)` instead of raw `tx_size` to compute the minimum fee, so enforcement is consistent with the fee rate's defined unit. If the cheap-check approximation is intentional, document it explicitly and ensure the RPC documentation reflects that the returned estimate may not match the enforcement threshold for cycle-heavy transactions.
3. Add explicit unit annotations to `FeeRate::from_u64` and `FeeRate::as_u64` to prevent callers from silently misinterpreting the raw `u64` value.

---

### Proof of Concept

Consider a transaction with `tx_size = 200` bytes and `cycles = 10_000_000`.

- `weight = max(200, 10_000_000 * 0.000_170_571_4) ≈ max(200, 1705) = 1705`
- `estimate_fee_rate` returns a rate based on weight, e.g., `R` shannons/KW
- Caller interprets `R` as shannons/KB and computes fee = `R * 200 / 1000 = R * 0.2`
- `check_tx_fee` enforces: `min_fee = min_fee_rate * 200 / 1000`
- If `R > min_fee_rate`, the transaction passes, but the caller paid `R * 0.2` instead of the weight-correct `R * 1.705` — a factor of ~8.5x underpayment relative to what the weight-based rate implies, or equivalently, the caller overpays relative to the size-based enforcement minimum.

The divergence grows linearly with the ratio `weight / size`, which is unbounded up to `max_tx_verify_cycles * DEFAULT_BYTES_PER_CYCLES / tx_size`. [11](#0-10) [12](#0-11) [4](#0-3)

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
```

**File:** util/types/src/core/fee_rate.rs (L11-16)
```rust
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
    }
```

**File:** util/types/src/core/fee_rate.rs (L23-26)
```rust
    /// Returns the fee rate as shannons per kilo-weight.
    pub const fn as_u64(self) -> u64 {
        self.0
    }
```

**File:** util/types/src/core/fee_rate.rs (L40-43)
```rust
impl ::std::fmt::Display for FeeRate {
    fn fmt(&self, f: &mut ::std::fmt::Formatter) -> ::std::fmt::Result {
        write!(f, "{} shannons/KW", self.0)
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

**File:** util/types/src/core/tx_pool.rs (L339-342)
```rust
    /// Fee rate threshold. The pool rejects transactions which fee rate is below this threshold.
    ///
    /// The unit is Shannons per 1000 bytes transaction serialization size in the block.
    pub min_fee_rate: FeeRate,
```

**File:** tx-pool/src/util.rs (L42-52)
```rust
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
```

**File:** rpc/src/module/experiment.rs (L189-191)
```rust
    /// ## Returns
    ///
    /// The estimated fee rate in shannons per kilobyte.
```

**File:** resource/ckb.toml (L212-214)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L469-472)
```rust
    pub fn accept_tx(&mut self, tx_hash: Byte32, info: TxEntryInfo) {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        self.track_tx(tx_hash, fee_rate, self.current_tip)
```
