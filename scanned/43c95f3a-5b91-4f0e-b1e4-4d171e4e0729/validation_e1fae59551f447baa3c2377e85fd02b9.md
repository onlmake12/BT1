### Title
Inconsistent Fee-Rate Formulas Between Admission Check and Pool Entry Calculation Allow `min_fee_rate` Bypass for Cycle-Heavy Transactions - (`tx-pool/src/util.rs`, `tx-pool/src/component/entry.rs`)

---

### Summary

The tx-pool admission check in `check_tx_fee` computes the minimum required fee using only the serialized transaction **size**, while the actual fee rate stored in every `TxEntry` and used for pool sorting/eviction uses `get_transaction_weight(size, cycles)` — which is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions the two formulas diverge significantly, allowing a transaction whose true weight-based fee rate is far below `min_fee_rate` to pass the admission gate.

---

### Finding Description

**Admission check** — `check_tx_fee` in `tx-pool/src/util.rs`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
```

`FeeRate::fee(weight)` is `rate * weight / 1000`, so the gate is:

```
fee  ≥  min_fee_rate × tx_size / 1000
```

i.e., the check is equivalent to `fee_rate_by_size ≥ min_fee_rate`. [1](#0-0) 

**Actual fee rate** — `TxEntry::fee_rate()` in `tx-pool/src/component/entry.rs`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

`get_transaction_weight` returns `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. [2](#0-1) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, so for any transaction where `cycles × 0.000_170_571_4 > size` the weight exceeds the size, and the true fee rate is strictly lower than the size-based rate used at admission. [3](#0-2) 

`FeeRate::calculate` and `FeeRate::fee` are the inverse of each other only when the same weight is used in both directions: [4](#0-3) 

The same weight-based formula is used consistently everywhere else — pool sorting (`AncestorsScoreSortKey`), eviction (`EvictKey`), fee-rate statistics (`FeeRateCollector`), and both fee-estimator algorithms — but **not** at the admission gate. [5](#0-4) [6](#0-5) 

---

### Impact Explanation

An attacker can craft a transaction with small serialized size but near-maximum cycles (up to `max_tx_verify_cycles = 70 000 000`) and pay a fee just above the size-based threshold.

**Concrete example** (default config: `min_fee_rate = 1 000 shannons/KW`):

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 70 000 000 |
| `weight` | `max(200, 70 000 000 × 0.000_170_571_4)` = **11 940** |
| Admission requires | `fee ≥ 1 000 × 200 / 1 000` = **200 shannons** |
| Actual fee rate | `200 × 1 000 / 11 940` ≈ **16.7 shannons/KW** |

A transaction paying 200 shannons passes the admission gate even though its true fee rate is ~16.7 shannons/KW — 60× below the configured `min_fee_rate` of 1 000 shannons/KW.

Consequences:
- The `min_fee_rate` anti-spam policy is bypassed for cycle-heavy transactions.
- An attacker can flood the pool with transactions whose actual fee rate is far below the minimum, consuming pool memory (up to `max_tx_pool_size = 180 MB`).
- These transactions are sorted to the bottom of the pool and will not be mined, but they displace legitimate transactions and degrade pool performance.
- The pool eviction logic (`EvictKey`) uses the correct weight-based fee rate, so these transactions are evicted last among equal-fee-rate entries, worsening the impact. [7](#0-6) 

---

### Likelihood Explanation

The attack is reachable by any unprivileged user via the `send_transaction` JSON-RPC endpoint or via P2P transaction relay. No special privileges, keys, or majority hash power are required. The attacker only needs to construct a valid transaction whose script consumes near-maximum cycles — a standard RISC-V script can be written to do this trivially. The discrepancy grows with cycles, so the bypass is most severe at the cycle limit, which is the easiest case to target.

---

### Recommendation

Replace the size-only check in `check_tx_fee` with the same weight formula used everywhere else. The cycles are available at the point `check_tx_fee` is called (they are passed in from the verified entry):

```rust
// In check_tx_fee, receive cycles as a parameter and compute weight consistently:
let weight = get_transaction_weight(tx_size, cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

This ensures the admission gate and the pool's internal fee-rate accounting use the same formula, eliminating the bypass.

---

### Proof of Concept

1. Construct a CKB transaction whose lock/type script loops until it consumes ~70 000 000 cycles. Keep the serialized transaction size small (e.g., 200 bytes by using a compact script reference via `code_hash`).
2. Set the output capacity so that `inputs_capacity - outputs_capacity = 200 shannons` (fee = 200 shannons).
3. Submit via `send_transaction` RPC to a node with default `min_fee_rate = 1 000`.
4. Observe the transaction is accepted (size-based check: `200 ≥ 1 000 × 200 / 1 000 = 200` — passes exactly).
5. Query `get_pool_tx_detail_info` and observe the entry's actual fee rate ≈ 16.7 shannons/KW, far below `min_fee_rate`.
6. Repeat to fill the pool with sub-minimum-fee-rate transactions.

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L221-247)
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
}

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

**File:** util/types/src/core/tx_pool.rs (L279-303)
```rust
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

**File:** util/types/src/core/fee_rate.rs (L11-37)
```rust
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

**File:** rpc/src/util/fee_rate.rs (L97-110)
```rust
                if let Some(cycles) = cycles {
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
                    }
                }
            }
            fee_rates
```
