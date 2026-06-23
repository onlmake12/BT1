### Title
`check_tx_fee` Enforces `min_fee_rate` Against Serialized Size Instead of Weight, Allowing Cycle-Heavy Transactions to Bypass the Minimum Fee Rate — (`tx-pool/src/util.rs`)

---

### Summary

`FeeRate` in CKB is defined as **shannons per kilo-weight**, where `weight = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. However, the sole pool-admission fee-rate gate (`check_tx_fee`) computes the minimum required fee using only `tx_size` (bytes), not `weight`. This is an exact metric-mismatch analog to the Llama H-01 finding: the threshold denominator (supply/weight) and the numerator being checked (approvals/fee) use different units, producing an incorrect comparison.

---

### Finding Description

`FeeRate` is defined as shannons per kilo-weight:

```rust
// util/types/src/core/fee_rate.rs
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;  // KW = 1000
    Capacity::shannons(fee)
}
```

`weight` is computed as:

```rust
// util/types/src/core/tx_pool.rs
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

When `cycles` dominate, `weight >> tx_size`. For example, a 200-byte transaction consuming 70,000,000 cycles (the default `max_tx_verify_cycles`) has:

- `weight = max(200, 70_000_000 × 0.000_170_571_4) ≈ max(200, 11_940) = 11_940`

But `check_tx_fee`, the **only** pool-admission fee-rate gate, uses `tx_size` instead of `weight`:

```rust
// tx-pool/src/util.rs
pub(crate) fn check_tx_fee(..., tx_size: usize) -> Result<Capacity, Reject> {
    // ...
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

With `min_fee_rate = 1000` shannons/KW and the example above:

| Metric | Value |
|---|---|
| Size-based min fee (what is checked) | `1000 × 200 / 1000 = 200 shannons` |
| Weight-based min fee (what should be checked) | `1000 × 11940 / 1000 = 11,940 shannons` |

A transaction paying 201 shannons passes admission but has an actual fee rate of `201 / 11940 × 1000 ≈ 16.8 shannons/KW` — **59× below** the configured `min_fee_rate`.

The same mismatch appears in `calculate_min_replace_fee` for RBF:

```rust
// tx-pool/src/pool.rs
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);  // size, not weight
    ...
}
```

After admission, all internal pool operations (sorting, eviction, block assembly) correctly use `get_transaction_weight`:

```rust
// tx-pool/src/component/entry.rs
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

This confirms the mismatch is isolated to the admission gate and the RBF extra-fee calculation.

---

### Impact Explanation

1. **Mempool pollution / tx-pool DoS**: An attacker submits many cycle-heavy, small-serialized-size transactions paying just above the size-based minimum fee. Each passes `check_tx_fee` but has a true weight-based fee rate far below `min_fee_rate`. These transactions occupy pool slots and consume CPU during script verification.

2. **Displacement of legitimate transactions**: When the pool reaches `max_tx_pool_size`, eviction is driven by weight-based fee rate (`EvictKey`). Legitimate transactions with moderate cycles but higher size-based fees may be evicted in favour of retaining the attacker's cycle-heavy entries if their weight-based fee rate happens to be comparable.

3. **Block cycle budget consumption at low cost**: If a miner's block assembler selects these transactions (they are in the pool and have valid proposals), they consume block cycle budget (`max_block_cycles`) while paying fees far below the operator's intended floor, reducing miner revenue per block.

4. **RBF weakening**: The `calculate_min_replace_fee` mismatch means a cycle-heavy replacement transaction needs to pay less extra fee than intended to displace an existing transaction.

---

### Likelihood Explanation

- Entry path is fully unprivileged: any actor can call `send_transaction` via JSON-RPC or relay a transaction over P2P.
- The attacker only needs to craft a transaction with a script that consumes many cycles (e.g., a loop-heavy RISC-V script) while keeping the serialized transaction small.
- The `max_tx_verify_cycles` default of 70,000,000 gives a weight multiplier of up to ~59× over size, making the bypass significant.
- The code comment explicitly acknowledges the mismatch ("Theoretically we cannot use size as weight directly"), confirming this is a known approximation that has real consequences.

---

### Recommendation

In `check_tx_fee`, after script verification has produced the actual cycle count, compute the minimum fee using `get_transaction_weight` instead of raw `tx_size`:

```rust
// tx-pool/src/util.rs
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
    cycles: Cycle,          // add actual cycles after verification
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

Apply the same fix to `calculate_min_replace_fee` in `tx-pool/src/pool.rs`, passing the replacement transaction's weight instead of its size.

Because `check_tx_fee` is called in `pre_check` before script execution (cycles are not yet known), the weight-based check should be deferred to a post-verification step, or a conservative upper-bound estimate using `max_tx_verify_cycles` can be used at pre-check time.

---

### Proof of Concept

**Setup**: Node with default config (`min_fee_rate = 1000` shannons/KW, `max_tx_verify_cycles = 70_000_000`).

**Craft the transaction**:
- Serialized size: ~200 bytes (minimal inputs/outputs/witnesses)
- Lock script: a RISC-V loop consuming ~70,000,000 cycles
- Fee: 201 shannons (just above size-based minimum: `1000 × 200 / 1000 = 200`)

**Expected (correct) behaviour**: Rejected with `LowFeeRate` because weight-based min fee = `1000 × 11940 / 1000 = 11,940 shannons > 201`.

**Actual behaviour**: Accepted into the pool. The transaction's true fee rate is `201 / 11940 × 1000 ≈ 16.8 shannons/KW`, far below the 1000 shannons/KW floor.

**Repeat** this submission in a loop to fill the pool with cycle-heavy, low-fee-rate transactions, displacing legitimate transactions and consuming block cycle budget at minimal cost.

---

**Root cause references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** tx-pool/src/pool.rs (L101-103)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
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
