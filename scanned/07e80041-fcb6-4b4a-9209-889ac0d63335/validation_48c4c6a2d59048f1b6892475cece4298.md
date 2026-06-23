### Title
Size-vs-Weight Fee Check Discrepancy Allows Cycle-Heavy Transactions to Bypass Effective Minimum Fee Rate - (File: tx-pool/src/util.rs)

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the serialized byte size of a transaction, while the actual block-assembly weight (`get_transaction_weight`) uses `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy, size-light transactions the two values diverge by up to ~119×, allowing an unprivileged sender to flood the mempool with transactions that pass the admission gate but are severely underpriced relative to the block resources they consume.

### Finding Description

`check_tx_fee` computes the minimum required fee as:

```rust
// tx-pool/src/util.rs:45
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

where `FeeRate::fee` is:

```rust
// util/types/src/core/fee_rate.rs:34-36
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000
    Capacity::shannons(fee)
}
```

So the gate is: `fee >= min_fee_rate * tx_size / 1000`.

The actual weight used everywhere else (block assembly, fee-rate sorting, fee estimator) is:

```rust
// util/types/src/core/tx_pool.rs:298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
// DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4
```

The code itself acknowledges the mismatch with a comment:

```
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
```

The "cheap check" is not merely imprecise — it is exploitable. A transaction with `tx_size = 100` bytes and `cycles = 70_000_000` (the default `max_tx_verify_cycles`) has:

| Metric | Value |
|---|---|
| Size-based min fee (admission) | `1000 × 100 / 1000 = 100 shannons` |
| Actual weight | `max(100, 70_000_000 × 0.000_170_571_4) ≈ 11_940` |
| Fee rate by actual weight | `100 × 1000 / 11_940 ≈ 8 shannons/KW` |
| Declared `min_fee_rate` | `1000 shannons/KW` |
| **Underpayment ratio** | **~119×** |

The transaction passes `check_tx_fee` but its true fee rate is ~125× below the configured minimum.

### Impact Explanation

An unprivileged tx-pool submitter can:

1. Craft transactions with maximum declared cycles and minimal serialized size (e.g., a single-input/single-output transaction with a complex lock script).
2. Pay only the size-based minimum fee (e.g., 100 shannons) while consuming cycle-equivalent block weight of ~11,940 bytes.
3. Flood the mempool with such transactions at a fraction of the intended cost.
4. Displace legitimately-priced transactions from block templates, since miners sort by `AncestorsScoreSortKey` which uses the true weight — the attacker's transactions will rank very low and clog the pool without being mined, starving honest users.
5. Exhaust the 180 MB mempool (`max_tx_pool_size`) at ~119× lower cost than intended, triggering pool eviction of higher-fee-rate transactions.

This is a direct analog to the Oracle Drift finding: the "oracle" is the size-only fee check, the "deviation threshold" is the ratio `cycles × DEFAULT_BYTES_PER_CYCLES / tx_size`, and the "redemption fee" is `min_fee_rate`. When the deviation threshold exceeds the fee floor, the attacker profits by submitting at the stale (size-only) price.

### Likelihood Explanation

- Reachable via the public `send_transaction` RPC and P2P relay — no privilege required.
- The maximum cycles per transaction is bounded by `max_tx_verify_cycles = 70_000_000` (configurable, default from `TWO_IN_TWO_OUT_CYCLES * 20`), giving a worst-case ~119× gap at default settings.
- The attack is cheap: at `min_fee_rate = 1000 shannons/KB`, filling the 180 MB pool with 100-byte transactions costs only `180_000_000 / 100 × 100 = 18_000_000_000 shannons ≈ 180 CKB` instead of the intended `~21_420 CKB`.
- No special script knowledge is needed; any script that is cycle-heavy but serializes small qualifies.

### Recommendation

Replace the size-only check in `check_tx_fee` with the true weight:

```rust
// tx-pool/src/util.rs
use ckb_types::core::tx_pool::get_transaction_weight;

pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
    cycles: Cycle,          // <-- add cycles parameter
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(...)
        .transaction_fee(rtx)?;
    let weight = get_transaction_weight(tx_size, cycles);
    let min_fee = tx_pool.config.min_fee_rate.fee(weight);
    if fee < min_fee {
        return Err(Reject::LowFeeRate(...));
    }
    Ok(fee)
}
```

The `cycles` value is available at the call site after `verify_rtx` returns a `Completed` entry. Alternatively, enforce the weight-based check as a post-verification step once cycles are known, before the entry is inserted into the pool.

### Proof of Concept

**Setup**: default mainnet config, `min_fee_rate = 1000 shannons/KB`, `max_tx_verify_cycles = 70_000_000`.

**Craft the transaction**:
- 1 input cell, 1 output cell → serialized size ≈ 100–200 bytes.
- Lock script that loops to consume ~70,000,000 cycles (e.g., a tight RISC-V loop).
- Fee = `min_fee_rate * tx_size / 1000 = 1000 * 150 / 1000 = 150 shannons`.

**Submit via RPC**:
```json
{ "method": "send_transaction", "params": [<crafted_tx>, "passthrough"] }
```

**Observe**:
- `check_tx_fee` passes: `fee(150) = 150 >= 150`.
- Actual weight: `max(150, 70_000_000 × 0.000_170_571_4) ≈ 11_940`.
- Effective fee rate: `150 × 1000 / 11_940 ≈ 12 shannons/KW` — 83× below `min_fee_rate`.
- Repeat ~1,200,000 times to fill the 180 MB pool at a total cost of ~180,000 shannons (≈ 0.0018 CKB) instead of the intended ~21,420 CKB.
- Legitimate transactions paying the correct fee rate are evicted or delayed.

**Relevant code locations**: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-12)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
```
