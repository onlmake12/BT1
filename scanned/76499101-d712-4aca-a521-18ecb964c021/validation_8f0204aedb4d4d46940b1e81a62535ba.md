Audit Report

## Title
`tx_size` Used Instead of `weight` in Fee-Rate Admission and RBF Checks, Allowing Cycle-Heavy Transactions to Bypass Minimum Fee Rate — (`tx-pool/src/util.rs`, `tx-pool/src/pool.rs`)

## Summary

`check_tx_fee()` passes raw serialized byte size (`tx_size`) to `FeeRate::fee()`, which is documented to expect a **weight** value (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). For a transaction consuming near-maximum cycles (~70M), weight can be ~60× larger than size, meaning the admission gate is computed against the wrong unit. There is no subsequent weight-based fee-rate check after `verify_rtx` returns the actual cycle count. The same flaw exists in `calculate_min_replace_fee()` for RBF checks. An attacker can force full CKB-VM script execution on the node at a fraction of the intended minimum fee cost.

## Finding Description

`FeeRate` is defined as shannons per kilo-weight, and `FeeRate::fee(weight)` computes `fee_rate × weight / 1000`. The correct weight is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`.

**Location 1 — `check_tx_fee`** (`tx-pool/src/util.rs:42-45`):
```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```
The comment acknowledges the mismatch. In `_process_tx` (`tx-pool/src/process.rs`), the flow is:
1. `pre_check()` → calls `check_tx_fee()` with `tx_size` (size-only check, passes)
2. `verify_rtx()` → runs full CKB-VM execution, returns actual `verified.cycles`
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` → entry created with actual cycles
4. `submit_entry()` → no weight-based fee-rate re-check

After `verify_rtx` returns the actual cycle count, there is no code path that computes `weight = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)` and re-validates `fee >= min_fee_rate.fee(weight)`. The entry is admitted directly.

**Location 2 — `calculate_min_replace_fee`** (`tx-pool/src/pool.rs:102-103`):
```rust
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```
Called from `check_rbf` at line 665 with `entry.size`. The `TxEntry` already has `entry.cycles` available (it is a fully-verified entry), so `get_transaction_weight(entry.size, entry.cycles)` could be used directly, but is not.

## Impact Explanation

This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

With default config (`min_fee_rate = 1000`, `max_tx_verify_cycles = 70_000_000`):
- A 200-byte transaction consuming 70M cycles has weight ≈ 11,940
- Correct minimum fee: `1000 × 11,940 / 1000 = 11,940 shannons`
- Actual check: `1000 × 200 / 1000 = 200 shannons`
- **Attacker pays ~200 shannons to force ~70M cycles of CKB-VM execution — a ~60× reduction in cost**

The attacker can continuously resubmit cycle-heavy transactions. Each submission passes `check_tx_fee` (200 ≥ 200 shannons), triggers a full `verify_rtx` execution (70M cycles of CKB-VM), and is admitted to the pool. The eviction mechanism uses actual weight, so these transactions are evicted first when the pool is full — but the attacker simply resubmits, forcing another full verification at minimal fee cost. This constitutes a low-cost DoS against node verification resources and pool throughput.

## Likelihood Explanation

Reachable by any unprivileged user via the `send_transaction` JSON-RPC endpoint. No special role, key, or configuration is required. The attacker only needs to craft a transaction with a lock script that loops for ~70M cycles while keeping the serialized transaction size small (~200 bytes). CKB-VM scripts with tight loops are straightforward to construct. The attack is repeatable with no meaningful rate-limiting at the fee level.

## Recommendation

**For `check_tx_fee`**: Since cycles are not yet known at pre-check time, use `max_tx_verify_cycles` as a conservative upper bound: replace `tx_pool.config.min_fee_rate.fee(tx_size as u64)` with `tx_pool.config.min_fee_rate.fee(get_transaction_weight(tx_size, tx_pool.config.max_tx_verify_cycles))`. Alternatively, restructure the flow to re-check the fee rate after `verify_rtx` returns the actual cycle count.

**For `calculate_min_replace_fee`**: The replacement entry's actual cycles are already known. Replace `self.config.min_rbf_rate.fee(size as u64)` with `self.config.min_rbf_rate.fee(get_transaction_weight(size, actual_cycles))`, where `actual_cycles` is passed from the `TxEntry`.

## Proof of Concept

1. Craft a CKB transaction with a lock script that loops for ~70,000,000 cycles and a serialized size of ~200 bytes.
2. Set the transaction fee to 200 shannons (satisfying `min_fee_rate.fee(200) = 200`).
3. Submit via `send_transaction` RPC.
4. The transaction passes `check_tx_fee` (200 ≥ 200), proceeds to `verify_rtx`, consumes ~70M cycles of CKB-VM execution, and is admitted to the pool.
5. The actual fee rate is `FeeRate::calculate(200_shannons, 11_940_weight) ≈ 16 shannons/kilo-weight`, far below the configured minimum of 1000.
6. Repeat continuously. Each iteration forces a full script verification at 200 shannons cost, keeping the pool filled with below-minimum-fee-rate cycle-heavy transactions.

For the RBF variant: submit a legitimate transaction, then submit a cycle-heavy replacement with fee = `sum(replaced_fees) + min_rbf_rate.fee(size)` instead of the correct `sum(replaced_fees) + min_rbf_rate.fee(weight)`, bypassing the RBF minimum increment check in `check_rbf` at `tx-pool/src/pool.rs:665`.