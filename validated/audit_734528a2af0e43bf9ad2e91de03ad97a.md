### Title
Tx-Pool Admission Fee-Rate Check Uses Size-Only Weight While Stored Fee Rate Uses Full Weight Formula, Allowing Below-`min_fee_rate` Transactions Into the Pool - (File: `tx-pool/src/util.rs`)

### Summary

The CKB tx-pool enforces `min_fee_rate` at admission using only the serialized transaction size as the weight denominator, but stores and reports each entry's fee rate using the full `get_transaction_weight(size, cycles)` formula — `max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For compute-heavy transactions where `cycles * DEFAULT_BYTES_PER_CYCLES > size`, the admission check passes at a higher effective threshold than the actual stored fee rate, allowing transactions whose true weight-based fee rate is below `min_fee_rate` to enter the pool. The `tx_pool_info` RPC then reports `min_fee_rate` as the enforced threshold, misleading callers about what is actually admitted.

### Finding Description

**Admission check** in `check_tx_fee` (`tx-pool/src/util.rs`, lines 28–54):

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { ... }
```

The minimum fee is computed as `min_fee_rate * tx_size / 1000` — using raw serialized size as the weight. [1](#0-0) 

**Stored fee rate** in `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs`, lines 114–118):

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

The stored fee rate uses `get_transaction_weight` = `max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. [2](#0-1) 

**Weight formula** (`util/types/src/core/tx_pool.rs`, lines 298–303):

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

For a transaction where `cycles * DEFAULT_BYTES_PER_CYCLES > tx_size`:

- **Admission check weight** = `tx_size` (smaller → easier to pass)
- **Actual stored weight** = `cycles * DEFAULT_BYTES_PER_CYCLES` (larger → lower actual fee rate)

A transaction with fee = `min_fee_rate * tx_size / 1000 + 1` passes admission, but its actual stored fee rate = `fee * 1000 / (cycles * DEFAULT_BYTES_PER_CYCLES)` is strictly below `min_fee_rate`.

**Reported threshold** via `tx_pool_info` RPC (`tx-pool/src/service.rs`, line 1091):

```rust
min_fee_rate: self.tx_pool_config.min_fee_rate,
``` [4](#0-3) 

The RPC reports `min_fee_rate` as the enforced threshold, but the actual admission check uses a different (looser) formula for compute-heavy transactions. This is the same class of inconsistency as the ERC4626 report: the "preview" (admission check) does not match the actual stored/reported value.

### Impact Explanation

1. **Below-threshold transactions enter the pool**: Any tx-pool submitter can craft a compute-heavy transaction (high cycles, low serialized size) with a fee just above `min_fee_rate * size / 1000` that passes admission but has an actual weight-based fee rate below `min_fee_rate`.
2. **Misleading RPC output**: `tx_pool_info` reports `min_fee_rate` as the threshold, but the pool already contains entries below it. Wallets and fee estimators that rely on this value to construct transactions will compute incorrect minimum fees.
3. **Fee estimator skew**: The fallback `estimate_fee_rate` in `pool_map.rs` iterates entries by their weight-based fee rate. If the pool contains entries admitted below `min_fee_rate` (by weight), the estimate can be skewed downward, causing further under-pricing. [5](#0-4) 

### Likelihood Explanation

Any unprivileged RPC caller submitting transactions via `send_transaction` can trigger this. CKB scripts (lock/type scripts) routinely consume significant cycles while having compact serialized transaction bodies. The condition `cycles * 0.000_170_571_4 > tx_size` is easily achievable with any moderately complex script. No special privileges, keys, or majority hashpower are required.

### Recommendation

Replace the size-only weight in `check_tx_fee` with the same `get_transaction_weight(tx_size, cycles)` formula used everywhere else. Since cycles are not yet known at the pre-check stage (verification happens after), the check should either:

1. Use the declared cycles (if provided by the submitter) for the weight calculation, or
2. Defer the fee-rate check until after script verification when actual cycles are known (in `_process_tx`, after `verify_rtx` returns `verified.cycles`), and reject there if the weight-based fee rate is below `min_fee_rate`.

This ensures the admission gate and the stored/reported fee rate use the same weight formula, eliminating the inconsistency.

### Proof of Concept

Consider `min_fee_rate = 1000 shannons/KW` and a transaction with:
- `tx_size = 200` bytes
- `cycles = 5_000_000` (so `cycles * DEFAULT_BYTES_PER_CYCLES ≈ 852` weight-bytes > 200)
- `fee = 201` shannons (just above `1000 * 200 / 1000 = 200`)

**Admission check** (`check_tx_fee`):
- `min_fee = 1000 * 200 / 1000 = 200`
- `fee (201) >= min_fee (200)` → **admitted**

**Stored fee rate** (`TxEntry::fee_rate`):
- `weight = max(200, 852) = 852`
- `fee_rate = 201 * 1000 / 852 ≈ 235 shannons/KW`
- `235 < 1000 (min_fee_rate)` → **below threshold**

The transaction is in the pool with an actual fee rate of ~235 shannons/KW, while `tx_pool_info` reports `min_fee_rate = 1000 shannons/KW`. [1](#0-0) [6](#0-5)

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

**File:** tx-pool/src/service.rs (L1091-1091)
```rust
            min_fee_rate: self.tx_pool_config.min_fee_rate,
```

**File:** tx-pool/src/component/pool_map.rs (L334-358)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
```
