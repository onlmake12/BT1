### Title
Tx-Pool Minimum Fee Admission Uses Serialized Size Only, Ignoring Cycle Cost — Allows High-Cycle Transactions to Bypass Fee Floor - (File: tx-pool/src/util.rs)

---

### Summary

The tx-pool admission gate `check_tx_fee` computes the minimum required fee using only the transaction's serialized byte size as the weight, explicitly ignoring the cycles dimension. A transaction with a tiny byte footprint but near-maximum cycle consumption passes the fee floor check while paying a fee that is orders of magnitude below what the actual resource cost warrants. This is the direct CKB analog of the Solidity `.transfer()` hardcoded gas stipend: a fixed, simplified resource metric is used in place of the true cost, and the gap can be exploited by any unprivileged submitter.

---

### Finding Description

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate by computing:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

where `tx_size` is the raw serialized byte length of the transaction. [1](#0-0) 

The code itself acknowledges the problem in a comment immediately above this line:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly"

The correct weight function, used everywhere else in the codebase (tx scoring, eviction, fee estimation), is `get_transaction_weight`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` is derived from `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES = 597_000 / 3_500_000_000`. [3](#0-2) 

The consensus constants that anchor this ratio are: [4](#0-3) 

The gap between the two metrics is large. For a transaction with the minimum viable byte size (~200 bytes) and the maximum allowed cycles (`max_tx_verify_cycles = 70_000_000`):

- **Correct weight**: `max(200, floor(70_000_000 × 0.000_170_571_4))` = `max(200, 11_939)` = **11,939**
- **Actual check weight**: `200` (byte size only)
- **Correct min fee** at 1,000 shannons/KW: **11,939 shannons**
- **Enforced min fee**: **200 shannons**
- **Underpayment ratio**: ~**60×**

The `max_tx_verify_cycles` default is `TWO_IN_TWO_OUT_CYCLES * 20 = 70_000_000`: [5](#0-4) 

---

### Impact Explanation

An attacker can submit a stream of transactions that are small in bytes but consume the maximum allowed cycles per transaction. Each transaction passes the fee floor check at ~60× below the fee that would be required if cycles were properly weighted. The verification worker pool (`max_tx_verify_workers`) is forced to execute expensive scripts for transactions that paid almost nothing. This enables:

1. **Tx-pool resource exhaustion**: verification workers are saturated with high-cycle work at negligible cost to the attacker.
2. **Fee market distortion**: high-cycle transactions displace legitimate transactions from the pool while appearing to have a competitive fee rate during admission, but their true resource cost is far higher.
3. **Degraded node throughput**: the node's ability to process and relay legitimate transactions is impaired.

---

### Likelihood Explanation

The entry path is fully open to any unprivileged actor via the `send_transaction` RPC or P2P relay. The attacker needs only to craft a transaction whose lock or type script consumes near-maximum cycles while keeping the serialized transaction small (e.g., a single input/output with a compact but computationally intensive script). The discrepancy is structural and deterministic — no race condition or timing dependency is required. The code comment explicitly acknowledges the approximation, confirming the gap is known but unmitigated at the admission gate.

---

### Recommendation

Replace the size-only weight in `check_tx_fee` with the same `get_transaction_weight(tx_size, cycles)` function used for scoring and eviction. Since cycles are not yet known at the pre-check stage (before script execution), the check should either:

1. Use a conservative upper-bound cycle estimate (e.g., `max_tx_verify_cycles`) to compute the weight for the fee floor, or
2. Re-run the fee check post-verification (after actual cycles are known) and reject the transaction if the fee is insufficient for its true weight.

Option 2 is more accurate and consistent with how `TxEntry::fee_rate()` is computed: [6](#0-5) 

---

### Proof of Concept

1. Craft a transaction with:
   - 1 input, 1 output (minimal serialized size, ~200 bytes)
   - A lock script that loops for ~70,000,000 cycles (near `max_tx_verify_cycles`)
   - Fee = 201 shannons (just above `min_fee_rate.fee(200)` = 200 shannons)

2. Submit via `send_transaction` RPC.

3. `check_tx_fee` computes `min_fee = 1000 * 200 / 1000 = 200 shannons`. The transaction passes admission.

4. The correct weight is `get_transaction_weight(200, 70_000_000)` = 11,939. The correct minimum fee is 11,939 shannons. The transaction paid only 201 shannons — ~59× below the proper threshold.

5. Repeat with many such transactions. Each occupies a verification worker for the full cycle budget while paying a negligible fee, exhausting node verification capacity.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** util/types/src/core/tx_pool.rs (L276-279)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
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

**File:** spec/src/consensus.rs (L69-84)
```rust
/// cycles of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_CYCLES: Cycle = 3_500_000;
/// bytes of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_BYTES: u64 = 597;
/// count of two-in-two-out txs a block should capable to package.
const TWO_IN_TWO_OUT_COUNT: u64 = 1_000;
pub(crate) const DEFAULT_EPOCH_DURATION_TARGET: u64 = 4 * 60 * 60; // 4 hours, unit: second
const MILLISECONDS_IN_A_SECOND: u64 = 1000;
const MAX_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MIN_BLOCK_INTERVAL; // 1800
const MIN_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MAX_BLOCK_INTERVAL; // 300
pub(crate) const DEFAULT_PRIMARY_EPOCH_REWARD_HALVING_INTERVAL: EpochNumber =
    4 * 365 * 24 * 60 * 60 / DEFAULT_EPOCH_DURATION_TARGET; // every 4 years

/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** util/app-config/src/legacy/tx_pool.rs (L14-14)
```rust
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
