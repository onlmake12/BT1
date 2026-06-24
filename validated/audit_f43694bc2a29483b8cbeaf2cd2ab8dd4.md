Audit Report

## Title
Tx-Pool Admission Uses Size-Only Fee Check, Bypassing Cycle-Based Weight — Allows Sub-`min_fee_rate` Transactions Into Pool - (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` enforces `min_fee_rate` using only serialized transaction size as the weight, while the canonical weight used everywhere else is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For high-cycles transactions, cycles dominate the true weight by up to ~60×. An unprivileged attacker can craft a transaction that passes the size-only admission gate but whose true fee rate is far below `min_fee_rate`, causing it to be admitted to the pool and relayed to all peers while consuming pool space with an unmineable transaction.

## Finding Description
In `tx-pool/src/util.rs` L45, `check_tx_fee` computes the minimum required fee as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment at L42–44 explicitly acknowledges this is intentional as a "cheap check." However, the canonical weight function `get_transaction_weight` in `util/types/src/core/tx_pool.rs` L298–303 is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

The full pipeline in `_process_tx` (`tx-pool/src/process.rs` L715–753) is:
1. `pre_check` → calls `check_tx_fee(tx_size)` — size-only gate, cycles unknown
2. `verify_rtx` — determines actual cycles
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with real cycles
4. `submit_entry` — admitted with no second fee-rate check

After `submit_entry`, `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs` L115–117) uses the true weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

But this is only consulted for eviction and sorting, never for admission gating. There is no second fee-rate check after cycles are known.

## Impact Explanation
An attacker submits a transaction with `tx_size ≈ 200` bytes and `cycles = 70,000,000` (the `max_tx_verify_cycles` default of `TWO_IN_TWO_OUT_CYCLES * 20`), paying `fee = 201 shannons`:

- Size-only check: `201 >= 1000 * 200 / 1000 = 200` → **passes**
- True weight: `max(200, 70,000,000 × 0.000_170_571_4) = 11,940`
- True fee rate: `201 × 1000 / 11,940 ≈ 16 shannons/KW` — **~62× below `min_fee_rate`**

The transaction is admitted, relayed to all peers, and occupies pool space. Miners will not include it. The attacker achieves ~60× amplification of pool space consumption per shannon paid. Continuously submitting such transactions keeps the pool polluted with unmineable entries, degrades mempool quality, and wastes relay bandwidth across the network. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
Any unprivileged user with access to the `send_transaction` RPC endpoint can exploit this. Crafting a high-cycles transaction requires only deploying a script that performs expensive computation up to the cycle limit — no special privileges, keys, or network position are required. The `max_tx_verify_cycles` default of `70,000,000` is reachable by any script author. The attack is repeatable as long as the attacker holds valid UTXOs and pays the (artificially low) size-based fee.

## Recommendation
After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the true weight before calling `submit_entry`:

```rust
let true_weight = get_transaction_weight(tx_size, verified.cycles);
let true_min_fee = tx_pool_config.min_fee_rate.fee(true_weight);
if fee < true_min_fee {
    return Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, true_min_fee.as_u64(), fee.as_u64()));
}
```

This mirrors how `TxEntry::fee_rate()` and `get_transaction_weight` already compute the canonical weight, and closes the gap between the admission check and the true economic cost of including the transaction.

## Proof of Concept
1. Deploy a CKB script that consumes close to `max_tx_verify_cycles` (70,000,000) cycles via a tight computation loop.
2. Construct a transaction using that script as the lock, with `tx_size ≈ 200` bytes.
3. Set the transaction fee to `201 shannons` (with default `min_fee_rate = 1000 shannons/KW`).
4. Submit via `send_transaction` RPC.
5. Observe: the transaction is accepted (size-only fee check passes: `201 >= 200`).
6. Compute true fee rate: `201 × 1000 / max(200, 70,000,000 × 0.000_170_571_4) = 201,000 / 11,940 ≈ 16 shannons/KW`.
7. Observe: the true fee rate (~16) is ~62× below `min_fee_rate` (1000), yet the transaction is relayed to peers and occupies pool space until evicted by the size limiter.
8. Repeat continuously to maintain pool pollution and saturate relay bandwidth.