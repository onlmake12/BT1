The code confirms the claim. Let me verify the full `_process_tx` flow to ensure no second fee check exists after verification.

Audit Report

## Title
Tx-Pool Minimum Fee Rate Admission Check Uses Serialized Size Instead of Actual Transaction Weight, Allowing Cycle-Heavy Transactions to Bypass Fee Enforcement - (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the transaction's serialized byte size, not the actual transaction weight (`max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`). After `verify_rtx` completes and actual cycles are known, no second fee check is performed. Any unprivileged caller can submit a cycle-heavy transaction with a fee far below the intended minimum, causing the node to expend significant CPU on script verification at a fraction of the intended cost.

## Finding Description
In `check_tx_fee`, the minimum fee threshold is computed as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code's own comment acknowledges this is a deliberate approximation: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."*

The actual transaction weight used everywhere else in the system is defined in `get_transaction_weight`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

In `_process_tx`, the flow is:
1. `pre_check` → calls `check_tx_fee` (size-based only, cycles unknown)
2. `verify_rtx` → actual cycles determined
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with real cycles
4. `submit_entry` — transaction admitted

There is no fee check between steps 2 and 4 using `verified.cycles`. The `TxEntry::fee_rate()` method correctly uses `get_transaction_weight(self.size, self.cycles)` for sorting and eviction, but this is never used as an admission gate. A transaction with 200 bytes serialized size and 70,000,000 cycles has an actual weight of `max(200, 11940) = 11940`, but the admission check uses only `200` — a ~60× undercount.

## Impact Explanation
An attacker can repeatedly submit transactions with minimal serialized size (~200 bytes) and maximum cycle consumption (~70M cycles), each paying only ~201 shannons (just above `min_fee_rate * 200 / 1000 = 200`). Each such transaction forces the node to run full script verification consuming up to 70M VM cycles of CPU. The node's verification worker pool becomes saturated with CPU-intensive work paid for at ~1.7% of the intended fee cost. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
Any unprivileged peer with access to the `send_transaction` RPC endpoint can trigger this. No special privilege, key, or majority hashpower is required. The attacker only needs to craft a transaction whose lock/type script consumes many cycles — straightforward for any script author. The default `max_tx_verify_cycles = 70,000,000` provides a large and fixed amplification factor. The attack is repeatable and cheap.

## Recommendation
After `verify_rtx` completes and actual cycles are known, perform a second fee check using the actual transaction weight before creating the `TxEntry`. In `_process_tx` (`tx-pool/src/process.rs`), between the `verify_rtx` result and `TxEntry::new`, add:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_actual = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < min_fee_actual {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

This mirrors the correct pattern already used in `TxEntry::fee_rate()` and `FeeRateCollector::statistics()`.

## Proof of Concept
1. Configure a CKB node with default `min_fee_rate = 1000` shannons/KW and `max_tx_verify_cycles = 70_000_000`.
2. Craft a transaction with serialized size ≈ 200 bytes, a lock script consuming ~70,000,000 cycles (tight computation loop in CKB-VM), and fee = 201 shannons.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 * 200 / 1000 = 200`; fee 201 ≥ 200 → **passes**.
5. `verify_rtx` runs the script consuming ~70M cycles; actual weight = `max(200, 11940) = 11940`.
6. Correct minimum fee should be `1000 * 11940 / 1000 = 11940` shannons — submitted fee of 201 is ~59× too low.
7. Transaction is admitted. Repeat to saturate verification workers and congest the node.