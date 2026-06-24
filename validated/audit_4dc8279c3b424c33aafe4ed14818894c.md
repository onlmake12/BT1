Audit Report

## Title
Tx-Pool Minimum Fee Check Uses Only Serialized Size, Ignoring Cycles Weight — Allows High-Cycles Transactions to Bypass Minimum Fee Rate - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces `min_fee_rate` using only the transaction's serialized byte size, not the true transaction weight `get_transaction_weight(size, cycles)`. Because `check_tx_fee` is called before script execution (cycles are unknown at that point), and no second fee-rate check is performed after `verify_rtx` returns the actual cycles, an attacker can submit a small-serialized, high-cycles transaction that passes the admission check while paying an effective fee rate orders of magnitude below the configured minimum. This enables low-cost CPU exhaustion and pool flooding attacks.

## Finding Description

In `tx-pool/src/util.rs` L42–45, `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment itself acknowledges the inconsistency. This function is called inside `pre_check` (process.rs L289/294) before script execution, so cycles are not yet available.

In `_process_tx` (process.rs L705–777), the flow is:
1. `pre_check` → `check_tx_fee` (size-only, L715–717)
2. `verify_rtx` → returns `verified.cycles` (L724–734)
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is created at L751
4. **No second fee-rate check using actual weight is performed**

Everywhere else in the system, the true weight is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

with `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. This weight is used by `TxEntry::fee_rate()` (entry.rs L115–117) and `AncestorsScoreSortKey` (entry.rs L221–231) for pool prioritization and eviction — but not for admission.

The same gap exists in `calculate_min_replace_fee` (pool.rs L103): `self.config.min_rbf_rate.fee(size as u64)` uses size only, so a high-cycles RBF replacement also underpays the surcharge.

## Impact Explanation

An attacker crafts a transaction with small serialized size (e.g., 300 bytes) referencing a cell dep containing a tight-loop CKB-VM script consuming ~69,000,000 cycles. The fee check passes at `min_fee_rate.fee(300) = 300 shannons`. The actual weight is `max(300, 69,000,000 × 0.000_170_571_4) ≈ 11,769 bytes`, giving an effective fee rate of ~25.5 shannons/KB — approximately 40× below the 1,000 shannons/KB minimum.

Such transactions:
1. Pass `check_tx_fee` and enter the pool
2. Consume significant validator CPU during `verify_rtx` (up to `max_block_cycles()` per transaction in the main `_process_tx` path, since L720 uses `self.consensus.max_block_cycles()`, not `max_tx_verify_cycles`)
3. Are sorted by their true (low) fee rate once in the pool, crowding out legitimate transactions
4. Can be submitted in bulk via RPC or P2P relay to exhaust pool capacity and validator CPU at a fraction of the intended cost

This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

Any unprivileged caller of `send_transaction` RPC or any P2P transaction relayer can trigger this. No special role, key, or majority hashpower is required. The attacker only needs to deploy a high-cycle CKB-VM script in a confirmed cell (keeping the transaction's serialized size small) and submit transactions with fees just above `min_fee_rate × tx_size`. The attack is repeatable, low-cost, and requires only standard CKB transaction submission access.

## Recommendation

After `verify_rtx` returns the actual cycles in `_process_tx`, perform a second fee-rate check using the true weight:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

Apply the same fix to `calculate_min_replace_fee` in `pool.rs`, replacing `self.config.min_rbf_rate.fee(size as u64)` with `self.config.min_rbf_rate.fee(get_transaction_weight(size, cycles))`, where `cycles` is the verified cycle count of the replacement transaction.

## Proof of Concept

1. Deploy a CKB-VM script that runs a tight loop consuming ~69,000,000 cycles. Store it in a confirmed cell (cell dep, not inline — keeping the transaction's serialized size ~300 bytes).
2. Construct a transaction spending any live cell, referencing the loop script as a type script, with `outputs_capacity = inputs_capacity − 300 shannons` (fee = 300 shannons).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = min_fee_rate.fee(300) = 300 shannons` → passes (L45, `tx-pool/src/util.rs`).
5. `verify_rtx` executes the script, consuming ~69M cycles (L724–732, `tx-pool/src/process.rs`).
6. `TxEntry` is created with `cycles ≈ 69,000,000` at L751; actual weight ≈ 11,769 bytes; effective fee rate ≈ 25.5 shannons/KB — far below the 1,000 shannons/KB minimum.
7. The transaction is accepted into the pool. Repeat in bulk to flood the pool with computationally expensive, underpaying transactions, exhausting validator CPU and pool capacity.