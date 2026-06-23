### Title
Minimum Fee Rate Check Uses Only Serialized Size, Ignoring Cycle-Based Weight — (`tx-pool/src/util.rs`)

### Summary

The `check_tx_fee` function enforces the minimum fee rate using only the transaction's serialized byte size (`tx_size`), while the actual transaction weight used for pool ordering and eviction is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. A transaction sender can craft a script that consumes many VM cycles but has a small serialized size, causing the transaction to pass the minimum fee check while its true effective fee rate is far below the configured minimum.

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

The true transaction weight, used everywhere else in the pool (sorting, eviction, fee estimation), is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. This weight is used in `TxEntry::fee_rate()` for pool ordering: [3](#0-2) 

The `check_tx_fee` call happens during `pre_check`, before VM execution determines actual cycles. The user-controlled parameter is the script's cycle consumption: by deploying a compact loop script (small serialized size, high cycle count), a sender makes `tx_size` small while `cycles` is large, so the weight-based fee rate is far below `min_fee_rate`.

**Concrete numbers with default config** (`min_fee_rate = 1_000` shannons/KB, `max_tx_verify_cycles = 70_000_000`): [4](#0-3) 

| Parameter | Value |
|---|---|
| `tx_size` | ~200 bytes (compact loop script tx) |
| `cycles` | 70,000,000 |
| `weight` | `max(200, 70_000_000 × 0.000_170_571_4)` = **11,940** |
| `min_fee` (size-only check) | `1_000 × 200 / 1_000` = **200 shannons** |
| Effective fee rate | `200 × 1_000 / 11_940` ≈ **16.7 shannons/KB** |
| Ratio vs. minimum | **~60× below minimum** |

The transaction passes admission but enters the pool with an effective fee rate ~60× below the configured minimum.

### Impact Explanation

An unprivileged transaction sender can flood the mempool with transactions that pay a tiny fraction of the minimum fee rate. Each such transaction occupies `max_tx_verify_cycles × DEFAULT_BYTES_PER_CYCLES` weight units of block space while paying only for its byte size. With `max_tx_pool_size = 180 MB` and a 60× fee discount, an attacker can occupy pool capacity that would normally require 60× more fee expenditure. Legitimate transactions paying the true minimum fee rate may be evicted or delayed, degrading mempool quality and miner revenue.

### Likelihood Explanation

The attack requires only:
1. Deploying a compact RISC-V loop script on-chain (trivial, one-time cost).
2. Submitting transactions via the standard `send_transaction` RPC or P2P relay — no privileged access required.

The code comment at the root cause site explicitly acknowledges the theoretical incorrectness of the size-only check, confirming this is a known gap rather than an oversight in the analysis. [5](#0-4) 

### Recommendation

Replace the size-only minimum fee check with the true weight:

```rust
// After VM verification, cycles are known; use true weight
let weight = get_transaction_weight(tx_size, verified_cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

Because cycles are not known at `pre_check` time (before VM execution), the check should be deferred to after `verify_rtx` returns `verified.cycles`, or a conservative upper-bound estimate using `max_tx_verify_cycles` should be applied at admission time as a stricter gate.

### Proof of Concept

1. Write a minimal RISC-V script that loops `N` times (e.g., `N = 69_000_000` to stay under `max_tx_verify_cycles = 70_000_000`). The compiled binary is ~150–300 bytes.
2. Deploy the script cell on-chain.
3. Construct a transaction referencing this script as a lock. The transaction's `tx_size` ≈ 300 bytes; its `cycles` ≈ 69,000,000.
4. Set `fee = min_fee_rate × tx_size / 1000 = 1_000 × 300 / 1_000 = 300 shannons`.
5. Submit via `send_transaction` RPC.
6. The `check_tx_fee` gate passes (`300 ≥ 300`).
7. After VM execution, `verified.cycles ≈ 69_000_000`; `weight = max(300, 11_769) = 11_769`; effective fee rate = `300 × 1_000 / 11_769 ≈ 25 shannons/KB` — **40× below the 1,000 shannons/KB minimum**.
8. The transaction is admitted to the pool. Repeat to fill the pool at a fraction of the intended cost.

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
