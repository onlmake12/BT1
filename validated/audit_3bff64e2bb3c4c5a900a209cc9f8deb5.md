Audit Report

## Title
`check_tx_fee` Underestimates Required Fee by Using `tx_size` Instead of `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`, Allowing `min_fee_rate` Bypass - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` gates pool admission using only `tx_size` as the fee weight denominator, while the authoritative weight function `get_transaction_weight` — used for pool sorting, eviction, and fee-rate display — returns `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. When a transaction's cycle cost is high enough that `cycles × DEFAULT_BYTES_PER_CYCLES > tx_size`, the admission check underestimates the required fee, allowing transactions whose actual fee rate is below `min_fee_rate` to enter the pool. An unprivileged attacker can exploit this to fill the tx-pool with artificially cheap-weight transactions, bypassing the primary DoS guard.

## Finding Description

**Admission check uses size only (`tx-pool/src/util.rs`, L42–45):**

The code explicitly acknowledges the inconsistency in a comment but proceeds with the cheaper calculation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The gate condition is therefore: `fee >= min_fee_rate × tx_size`.

**Canonical weight function uses the maximum of two metrics (`util/types/src/core/tx_pool.rs`, L298–303):**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (L279).

**Admission flow runs the fee check before script execution (`tx-pool/src/process.rs`, L715–751):**

`pre_check` (which calls `check_tx_fee`) runs at L715 before `verify_rtx` at L724–732, so actual cycles are unknown at the time of the fee gate. After admission, `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs`, L115–118) computes the real fee rate using `get_transaction_weight(self.size, self.cycles)` — the max formula — which can be far larger than `tx_size` alone.

**No post-verification fee check exists:** After `verify_rtx` returns with actual cycles at L734, the code proceeds directly to `TxEntry::new(rtx, verified.cycles, fee, tx_size)` at L751 with no second fee check using the real weight. `get_transaction_weight` is only referenced in `tx-pool/src/component/entry.rs`, never in `process.rs`.

**The mismatch:**

| Stage | Weight used | Formula |
|---|---|---|
| `check_tx_fee` (gate) | `tx_size` | size only |
| `TxEntry::fee_rate()` (actual) | `get_transaction_weight` | `max(size, cycles × k)` |

**Concrete amplification:**
- `tx_size = 1,000` bytes, `cycles = 70,000,000` (the `max_tx_verify_cycles` default)
- `cycles × DEFAULT_BYTES_PER_CYCLES ≈ 11,940` bytes
- Attacker pays fee for weight `1,000`, but occupies pool weight `11,940` — a ~12× amplification

## Impact Explanation

This matches the High-severity bounty impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** An attacker can continuously submit transactions paying only `min_fee_rate × tx_size` shannons — the cheapest possible admission cost — while each transaction occupies up to ~12× more pool weight than paid for. This fills the pool with artificially cheap-weight entries, displacing or delaying legitimate transactions and effectively nullifying the `min_fee_rate` spam-protection threshold.

## Likelihood Explanation

- **Entry path**: any RPC caller (`send_transaction`) or P2P relay peer — no privilege required.
- **Craft requirement**: a valid CKB script consuming many cycles. Loop-heavy RISC-V scripts trivially achieve `max_tx_verify_cycles ≈ 70,000,000` cycles.
- **Cost**: `min_fee_rate × tx_size` shannons per transaction — minimum possible admission cost.
- **Always active**: the condition `cycles × DEFAULT_BYTES_PER_CYCLES > tx_size` is achievable by any script author and requires no special network state.
- **Repeatable**: the attacker can continuously re-submit; pool eviction uses real weight but the attacker can keep refilling at low cost.

## Recommendation

Move the authoritative fee-rate check to **after** script execution, where actual cycles are known, and compute `min_fee` using `get_transaction_weight(tx_size, verified.cycles)`:

```rust
// After verify_rtx returns verified.cycles:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

For the relay path where `declared_cycles` is provided before execution, a preliminary check using `declared_cycles` can serve as an early rejection, with the authoritative check using actual cycles after verification.

## Proof of Concept

1. Author a CKB script that runs a tight loop consuming ~70,000,000 cycles. Deploy it on devnet.
2. Construct a transaction spending a cell locked by that script. Set `tx_size ≈ 1,000` bytes.
3. Set the transaction fee to exactly `min_fee_rate × 1,000` shannons (e.g., `1,000 shannons/KW × 1,000 bytes / 1,000 = 1,000 shannons`).
4. Submit via `send_transaction` RPC.
5. Observe: `check_tx_fee` passes (`fee >= min_fee_rate × tx_size`).
6. After script execution, `TxEntry::fee_rate()` returns `1,000 / 11,940 ≈ 83 shannons/KW` — well below `min_fee_rate = 1,000 shannons/KW`.
7. The transaction is now in the pool with an actual fee rate ~12× below the configured minimum. Repeat to fill the pool.