Audit Report

## Title
`check_tx_fee` Uses Serialized Size Instead of Canonical Weight for Min-Fee-Rate Enforcement, Allowing Sub-Threshold Transactions Into the Mempool — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces `min_fee_rate` using only the transaction's serialized byte size, while the canonical transaction weight used everywhere else in the codebase is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. An unprivileged attacker can craft a transaction with a small serialized size but near-maximum script execution cycles, pay a fee that satisfies only the size-based minimum, and have the transaction admitted to the pool with an effective fee rate up to ~119× below the configured `min_fee_rate`. No second fee-rate check is performed after script execution reveals the actual cycles.

## Finding Description

**Root cause — size-only fee check:**

In `tx-pool/src/util.rs` at line 45, `check_tx_fee` computes:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

where `tx_size = tx.data().serialized_size_in_block()`. The code itself acknowledges the discrepancy at lines 42–44:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
```

**Canonical weight function (not used in fee check):**

`get_transaction_weight` in `util/types/src/core/tx_pool.rs` lines 298–303:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

This same function is used in `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs` lines 115–118), `AncestorsScoreSortKey` (lines 221–232), and `EvictKey` (lines 234–247).

**Execution order — no post-verification fee check:**

In `tx-pool/src/process.rs`, `_process_tx` calls `pre_check` (which calls `check_tx_fee`) at line 715, then calls `verify_rtx` at line 724. After `verify_rtx` returns the actual cycles at line 734, the code only checks for declared-vs-actual cycle mismatch (lines 736–748) and then creates the `TxEntry` with actual cycles at line 751. There is no second fee-rate check against the true weight.

**Result:** A transaction with `tx_size = 100` bytes and `cycles = 70,000,000` passes `check_tx_fee` with a fee of 100 shannons (satisfying `min_fee_rate = 1000 shannons/KW` on size alone), but its actual weight is `max(100, 70_000_000 × 0.000_170_571_4) ≈ 11,940` bytes, giving an effective fee rate of `100 × 1000 / 11,940 ≈ 8.4 shannons/KW` — approximately 119× below the configured threshold.

## Impact Explanation

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

An attacker can flood the mempool with transactions whose effective fee rates are far below `min_fee_rate`. With the default 180 MB pool size and ~100-byte transactions, the pool can hold ~1.8 million such transactions. Each costs only 100 shannons in fees (vs. the ~11,940 shannons that would be required if weight were used). This degrades fee estimation accuracy for all users, displaces legitimate transactions from the pool, and consumes mempool resources at a fraction of the intended cost. While the eviction mechanism (`limit_size` → `next_evict_entry`) correctly uses weight-based fee rate and would evict these transactions first when the pool is full, an attacker who continuously resubmits can maintain a persistent presence and sustain the degradation.

## Likelihood Explanation

Any unprivileged transaction sender can exploit this. The attacker deploys a complex lock or type script (once, as a cell dep) consuming near-`max_tx_verify_cycles` cycles, then submits many transactions referencing it. Each transaction has small serialized size but high script execution cycles. The only recurring cost is the size-based minimum fee (e.g., 100 shannons per transaction) and having valid input cells to spend. This is reachable via the `send_transaction` RPC or P2P relay (`submit_remote_tx`). The attack is repeatable and requires no special privileges.

## Recommendation

Replace the size-only proxy in `check_tx_fee` with the canonical weight function. Since actual cycles are unknown at pre-check time, use the declared cycles (available for remote transactions via the `declared_cycles` parameter in `_process_tx`) or a conservative upper bound (`max_tx_verify_cycles`) for local transactions:

```rust
let weight = get_transaction_weight(tx_size, declared_cycles.unwrap_or(max_tx_verify_cycles));
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

Alternatively, perform a second fee-rate check after `verify_rtx` returns the actual cycles, rejecting transactions whose true effective fee rate falls below `min_fee_rate`.

## Proof of Concept

1. Deploy a lock script that consumes ~70,000,000 cycles (e.g., a tight computation loop) as a cell dep on a local node.
2. Construct a transaction spending a cell locked by that script. The transaction body (inputs, outputs, witnesses) is ~100 bytes serialized.
3. Set the transaction fee to exactly `ceil(min_fee_rate × tx_size / 1000) = 100 shannons` (for `min_fee_rate = 1000`, `tx_size = 100`).
4. Submit via `send_transaction` RPC.
5. `check_tx_fee` passes: `fee (100) >= min_fee (100)`.
6. `verify_rtx` executes the script, consuming ~70,000,000 cycles.
7. The transaction is admitted to the pool. Inspect via `get_transaction` RPC — the entry has `cycles ≈ 70,000,000`, `size ≈ 100`, `fee = 100 shannons`, giving effective fee rate ≈ 8.4 shannons/KW.
8. Repeat with many input cells to fill the mempool with sub-threshold-fee-rate transactions and observe fee estimation degradation via `estimate_fee_rate` RPC.