### Title
Unit Mismatch Between `shannons/KB` and `shannons/KW` in Tx-Pool Fee Enforcement Allows Cycle-Heavy Transactions to Bypass Minimum Fee Rate - (File: tx-pool/src/pool.rs, tx-pool/src/util.rs)

---

### Summary

`FeeRate` is defined as **shannons per kilo-weight** (shannons/KW), where `weight = max(size_bytes, cycles × DEFAULT_BYTES_PER_CYCLES)`. However, both the minimum-fee admission check (`check_tx_fee`) and the RBF minimum-replacement-fee calculation (`calculate_min_replace_fee`) pass raw serialized **size in bytes** as the weight argument. For cycle-heavy transactions where `weight >> size`, this unit mismatch causes the enforced minimum fee to be far lower than the configured threshold, allowing such transactions to enter the pool with an actual fee rate well below `min_fee_rate`, and to replace existing transactions via RBF while paying far less extra fee than intended.

---

### Finding Description

**`FeeRate` unit definition:**

`FeeRate` is documented and implemented as shannons per kilo-weight:

```rust
/// shannons per kilo-weight
pub struct FeeRate(pub u64);
const KW: u64 = 1000;

pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;
    Capacity::shannons(fee)
}
```

**`get_transaction_weight` definition:**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

For a cycle-heavy transaction, `weight` can be many times larger than `size`.

**Mismatch site 1 — `check_tx_fee` (tx-pool/src/util.rs, line 45):**

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

`tx_size` (bytes) is passed where `weight` is expected. The comment acknowledges the mismatch but treats it as acceptable. For a cycle-heavy transaction with `weight = N × size`, the enforced minimum fee is `N` times lower than the configured `min_fee_rate` would require.

**Mismatch site 2 — `calculate_min_replace_fee` (tx-pool/src/pool.rs, line 103):**

```rust
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

Again, raw `size` (bytes) is passed as weight. Unlike `check_tx_fee`, there is **no comment** acknowledging this as intentional. For a cycle-heavy replacement transaction where `weight >> size`, the required extra RBF fee is a fraction of what `min_rbf_rate` is intended to enforce.

**Config documentation reinforces the confusion:**

```toml
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
```

The config comment says shannons/KB, but the type is shannons/KW. These are only equivalent when `weight == size` (i.e., when the transaction is not cycle-heavy).

---

### Impact Explanation

**Tx-pool admission bypass:** A cycle-heavy transaction with `weight = K × size` (K > 1) passes `check_tx_fee` by paying only `min_fee_rate × size / 1000` shannons, while its actual fee rate (computed correctly as `fee × 1000 / weight`) is `K` times lower than `min_fee_rate`. The transaction enters the pool with a sub-minimum actual fee rate.

**RBF extra-fee underpayment:** When replacing an existing transaction, the attacker's cycle-heavy replacement must pay only `sum(replaced_fees) + min_rbf_rate × size / 1000`. The correct requirement should be `sum(replaced_fees) + min_rbf_rate × weight / 1000`. For `weight = K × size`, the attacker pays `K` times less extra fee than intended. The replacement enters the pool with a lower actual fee rate than the transaction it displaced, reducing miner revenue per unit of block space consumed.

**Block assembly impact:** Block assembly uses weight-based fee rate for prioritization. Cycle-heavy transactions admitted via the size-based cheap check will be deprioritized, potentially never being mined, while having consumed pool space and displaced other transactions via RBF.

---

### Likelihood Explanation

Any unprivileged tx-pool submitter can craft a cycle-heavy transaction by including a script that performs many VM cycles while keeping the serialized transaction size small. CKB-VM scripts are user-defined, so this is straightforward. The attacker submits such a transaction via the `send_transaction` RPC or P2P relay. No special privileges, keys, or majority hashpower are required.

---

### Recommendation

Replace raw `size` with `get_transaction_weight(size, cycles)` in both enforcement sites:

1. In `check_tx_fee` (`tx-pool/src/util.rs`): compute `weight = get_transaction_weight(tx_size, cycles)` and use it in `min_fee_rate.fee(weight)`.
2. In `calculate_min_replace_fee` (`tx-pool/src/pool.rs`): accept `cycles` alongside `size` and compute `weight = get_transaction_weight(size, cycles)` before calling `min_rbf_rate.fee(weight)`.

Alternatively, document explicitly that `min_fee_rate` and `min_rbf_rate` are intentionally enforced in shannons/KB (not shannons/KW) and update the `FeeRate` type documentation and RPC documentation to reflect this consistently.

---

### Proof of Concept

1. Craft a transaction `tx_cycle_heavy` with:
   - Serialized size `S = 200` bytes
   - A script consuming `C = 10,000,000` cycles
   - `weight = max(200, 10_000_000 × 0.000_170_571_4) ≈ 1705` weight-units
   - Fee `F = min_fee_rate × S / 1000 = 1000 × 200 / 1000 = 200` shannons

2. Submit via `send_transaction` RPC. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200` shannons. Since `F = 200 ≥ 200`, the transaction is admitted.

3. Actual fee rate = `200 × 1000 / 1705 ≈ 117` shannons/KW — well below the configured `min_fee_rate = 1000` shannons/KW.

4. For RBF: submit `tx_original` (fee = 500 shannons, size = 200 bytes). Then submit `tx_cycle_heavy` (same inputs, fee = 500 + 1500 × 200 / 1000 = 800 shannons). The RBF check passes. The correct extra fee should be `1500 × 1705 / 1000 ≈ 2557` shannons, but only 300 shannons extra was required — an ~8.5× underpayment. `tx_original` (actual fee rate ≈ 2500 shannons/KW) is evicted and replaced by `tx_cycle_heavy` (actual fee rate ≈ 469 shannons/KW), reducing miner revenue per block space.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** resource/ckb.toml (L211-214)
```text
max_tx_pool_size = 180_000_000 # 180mb
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
```
