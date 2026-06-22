### Title
Tx-Pool Minimum Fee Rate Check Uses `tx_size` Instead of Actual Weight, Allowing High-Cycle Transactions to Bypass Spam Protection — (File: `tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the serialized byte size of a transaction (`tx_size`), ignoring the cycle-based component of the transaction weight. The correct weight formula is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. Any transaction sender or relaying peer can craft a script-heavy (high-cycle), byte-small transaction that passes the minimum fee rate gate with an effective fee rate far below the configured threshold.

---

### Finding Description

CKB's transaction weight is defined in `util/types/src/core/tx_pool.rs` as:

```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [1](#0-0) 

This weight is used correctly everywhere else — in `TxEntry::fee_rate()`, in `AncestorsScoreSortKey`, in `EvictKey`, and in `FeeRateCollector::statistics()`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [2](#0-1) 

However, the **admission gate** `check_tx_fee` does not use `get_transaction_weight`. It uses only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [3](#0-2) 

`FeeRate::fee(weight)` expands to `fee_rate * weight / 1000`. [4](#0-3) 

So the check enforces: `fee >= min_fee_rate * tx_size / 1000`

But the actual fee rate of the transaction (used for eviction and block selection) is: `fee * 1000 / max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`

When `cycles * DEFAULT_BYTES_PER_CYCLES >> tx_size`, the two diverge dramatically.

**Concrete numbers** with default config (`min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70_000_000`):

| Parameter | Value |
|---|---|
| Typical tx size | ~242 bytes |
| Max weight from cycles | `70_000_000 × 0.000_170_571_4 ≈ 11,940` bytes |
| `min_fee` required by check | `1000 × 242 / 1000 = 242 shannons` |
| Correct min fee (by weight) | `1000 × 11940 / 1000 = 11,940 shannons` |
| Actual fee rate if fee = 242 | `242 × 1000 / 11940 ≈ 20 shannons/KW` |

A transaction paying only 242 shannons passes the gate, yet its real fee rate is ~20 shannons/KW — 50× below the 1000 shannons/KW minimum.

The config file comment even confirms the intended unit is size-based, not weight-based:

```toml
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
``` [5](#0-4) 

---

### Impact Explanation

- Transactions with scripts consuming up to `max_tx_verify_cycles` cycles but small serialized size enter the tx pool with an effective fee rate up to ~50× below the configured minimum.
- This undermines the `min_fee_rate` spam-protection invariant: the pool can be filled with low-fee-rate, high-CPU-cost transactions.
- Each such transaction forces the node to execute expensive script verification (up to 70M cycles), while paying only the fee required for a ~242-byte transaction.
- These transactions are also relayed to peers (the relay path calls the same `check_tx_fee`), propagating the underpriced load across the network.
- Once in the pool, these transactions are correctly ranked by actual weight for eviction, but they have already consumed verification CPU and pool slots.

**Impact: Medium** — pool spam protection is bypassed; CPU cost per admitted transaction can be ~50× higher than the fee implies.

---

### Likelihood Explanation

- Entry path requires no privilege: any `send_transaction` RPC caller or P2P transaction relay peer qualifies.
- Crafting a high-cycle, small-size transaction requires writing a CKB script (e.g., a lock script with a tight loop), which is a standard capability for any script author.
- The default `max_tx_verify_cycles = 70_000_000` is large enough to make the weight ratio ~49×.
- The code comment explicitly acknowledges the approximation, indicating the gap is known but unmitigated.

**Likelihood: Medium**

---

### Recommendation

Replace the size-only weight in `check_tx_fee` with the actual transaction weight. Since cycles are not yet known at the pre-verification admission stage, the check should be applied **after** script execution (where cycles are known), or a conservative upper-bound weight should be used. The correct fix mirrors how `TxEntry::fee_rate()` computes weight:

```rust
// After cycles are known from verification:
let weight = get_transaction_weight(tx_size, cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

This aligns the admission check with the fee rate used for eviction, block selection, and fee statistics.

---

### Proof of Concept

1. Author a CKB lock script that loops for ~70M cycles but compiles to a small binary (e.g., a tight RISC-V loop in ~100 bytes of code).
2. Create a transaction spending a cell locked by this script. The transaction's serialized size is ~242 bytes.
3. Set the output capacity equal to `input_capacity - 242 shannons` (fee = 242 shannons).
4. Submit via `send_transaction` RPC to a node with `min_fee_rate = 1000`.
5. **Expected (correct) behavior**: rejected with `LowFeeRate` because actual weight ≈ 11,940 and required fee = 11,940 shannons.
6. **Actual behavior**: accepted, because `check_tx_fee` computes `min_fee = 1000 × 242 / 1000 = 242 shannons` and `fee (242) >= min_fee (242)`.
7. The node spends ~70M cycles verifying the script for a transaction that paid only the minimum fee for a 242-byte, zero-cycle transaction.

Repeat with many such transactions to exhaust pool capacity and peer relay bandwidth at a fraction of the intended cost.

### Citations

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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** resource/ckb.toml (L212-214)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
```
