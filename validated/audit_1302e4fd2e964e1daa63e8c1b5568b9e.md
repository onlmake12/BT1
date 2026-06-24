Audit Report

## Title
Tx-Pool Minimum Fee Check Uses Only Serialized Size, Ignoring Cycles Weight — Allows High-Cycles Transactions to Bypass Minimum Fee Rate - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size, not the true transaction weight `get_transaction_weight(size, cycles)`. Because `check_tx_fee` is called before `verify_rtx` (which determines actual cycles), and no second fee-rate check is performed after verification, a transaction with small serialized size but near-maximum cycles can enter the pool with an effective fee rate orders of magnitude below `min_fee_rate`. This enables low-cost flooding of the pool with computationally expensive transactions.

## Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` (L28–54) computes the minimum required fee as:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment itself acknowledges the inconsistency. The weight denominator is `tx_size` (serialized bytes only).

Everywhere else in the system — `TxEntry::fee_rate()` (`entry.rs` L115–118), `AncestorsScoreSortKey` (`entry.rs` L221–231), eviction (`entry.rs` L236–238) — the weight is computed as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

with `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (`util/types/src/core/tx_pool.rs` L279).

In `_process_tx` (`process.rs` L705–777), the call order is:

1. **L715**: `pre_check(&tx)` → calls `check_tx_fee` with `tx_size` only (cycles unknown at this point).
2. **L724–732**: `verify_rtx(...)` → returns `verified.cycles` (actual cycles now known).
3. **L751**: `TxEntry::new(rtx, verified.cycles, fee, tx_size)` → no second fee-rate check using `get_transaction_weight(tx_size, verified.cycles)`.

The gap is that after `verify_rtx` returns the actual cycles, the code creates the `TxEntry` and submits it directly without re-validating the fee rate against the true weight. The same inconsistency exists in `calculate_min_replace_fee` (`pool.rs` L103):

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

This also uses `size` only, not `get_transaction_weight(size, cycles)`.

## Impact Explanation

An attacker submits a transaction with small serialized size (e.g., 300 bytes) but near-maximum cycles (e.g., 69,000,000). The fee paid is `min_fee_rate.fee(300) = 300 shannons` (at default 1,000 shannons/KB). The actual weight is `max(300, 69,000,000 × 0.000_170_571_4) ≈ 11,769 bytes`, giving an effective fee rate of `300 / 11,769 × 1,000 ≈ 25.5 shannons/KB` — approximately **40× below the configured minimum**.

Such transactions:
1. Pass `check_tx_fee` and enter the pool.
2. Consume significant validator CPU (up to 70M cycles per tx) during `verify_rtx`.
3. Are sorted by the pool using their true (low) fee rate, crowding out legitimate transactions.
4. Can be submitted in bulk via RPC or P2P relay to exhaust pool capacity and validator CPU at a fraction of the intended cost.

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation

Any unprivileged `send_transaction` RPC caller or P2P transaction relayer can exploit this. No special role, key, or majority hashpower is required. The attacker only needs to deploy a CKB-VM script that consumes many cycles (e.g., a tight loop) stored as a cell dep (keeping the transaction's serialized size small), then submit transactions with a fee just above `min_fee_rate × tx_size`. The attack is repeatable and low-cost.

## Recommendation

After `verify_rtx` returns the actual cycles in `_process_tx`, perform a second fee-rate check using the true weight:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

Apply the same fix to `calculate_min_replace_fee` in `pool.rs`, replacing `self.config.min_rbf_rate.fee(size as u64)` with `self.config.min_rbf_rate.fee(get_transaction_weight(size, cycles))`, where `cycles` is the verified cycle count of the replacement transaction.

## Proof of Concept

1. Deploy a CKB-VM script that runs a tight loop consuming ~69,000,000 cycles. Store it in a confirmed cell as a cell dep (keeping the transaction's serialized size small, e.g., ~300 bytes).
2. Construct a transaction spending any live cell, referencing the loop script as a type script, with `outputs_capacity = inputs_capacity − 300 shannons` (fee = 300 shannons).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = min_fee_rate.fee(300) = 300 shannons` → passes (`tx-pool/src/util.rs` L45).
5. `verify_rtx` executes the script, consuming ~69M cycles (`tx-pool/src/util.rs` L85–131).
6. `TxEntry` is created with `cycles ≈ 69,000,000` and no second fee check (`tx-pool/src/process.rs` L751). Actual weight ≈ 11,769 bytes; effective fee rate ≈ 25.5 shannons/KB — far below the 1,000 shannons/KB minimum.
7. The transaction is accepted into the pool. Repeat to flood the pool with computationally expensive, underpaying transactions.